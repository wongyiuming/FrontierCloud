/* Bounded HTTP I/O observation. Never reads, copies or retains payloads. */
#include <ngx_config.h>
#include <ngx_core.h>
#include <ngx_http.h>
#include <signal.h>
#include <errno.h>

#define FC_BUCKETS 50
#define FC_WORKERS 1024
#define FC_MAGIC UINT64_C(0x4643524541445632)
typedef struct { uint64_t tick, in, out; } fc_bucket_t;
typedef struct { ngx_pid_t pid; uint64_t active; } fc_worker_t;
typedef struct {
    uint64_t magic;
    fc_bucket_t buckets[FC_BUCKETS];
    fc_worker_t workers[FC_WORKERS];
    ngx_uint_t incomplete;
    ngx_msec_t created;
    ngx_pid_t creator;
} fc_shared_t;
typedef struct { ngx_shm_zone_t *zone; fc_shared_t *shared; } fc_conf_t;
typedef struct {
    ngx_queue_t link;
    ngx_http_request_t *request;
    fc_conf_t *conf;
    off_t received, sent;
    ngx_uint_t counted;
} fc_request_t;

static ngx_queue_t fc_active;
static ngx_event_t fc_timer;
static ngx_int_t fc_slot = -1;
static fc_conf_t *fc_worker_conf;
static ngx_int_t fc_init(ngx_conf_t *cf);
static ngx_int_t fc_process(ngx_cycle_t *cycle);
static void fc_exit(ngx_cycle_t *cycle);
static void *fc_create_conf(ngx_conf_t *cf);
static char *fc_enable(ngx_conf_t *cf, ngx_command_t *cmd, void *conf);
static char *fc_status_conf(ngx_conf_t *cf, ngx_command_t *cmd, void *conf);
static ngx_int_t fc_status(ngx_http_request_t *r);
static ngx_int_t fc_start(ngx_http_request_t *r);
static void fc_cleanup(void *data);
static void fc_sample(fc_request_t *ctx);
static void fc_tick(ngx_event_t *ev);
static ngx_http_output_body_filter_pt fc_next_body;
static ngx_http_request_body_filter_pt fc_next_input;

static ngx_command_t fc_commands[] = {
    { ngx_string("fc_business_traffic"), NGX_HTTP_MAIN_CONF|NGX_CONF_NOARGS,
      fc_enable, NGX_HTTP_MAIN_CONF_OFFSET, 0, NULL },
    { ngx_string("fc_business_traffic_status"), NGX_HTTP_LOC_CONF|NGX_CONF_NOARGS,
      fc_status_conf, NGX_HTTP_LOC_CONF_OFFSET, 0, NULL },
    ngx_null_command
};
static ngx_http_module_t fc_module_ctx = {
    NULL, fc_init, fc_create_conf, NULL, NULL, NULL, NULL, NULL
};
ngx_module_t ngx_http_fc_traffic_module = {
    NGX_MODULE_V1, &fc_module_ctx, fc_commands, NGX_HTTP_MODULE,
    NULL, NULL, fc_process, NULL, NULL, fc_exit, NULL, NGX_MODULE_V1_PADDING
};

static uint64_t fc_add(uint64_t a, uint64_t b) {
    return b > UINT64_MAX-a ? UINT64_MAX : a+b;
}
/* A dead worker cannot retain a file descriptor. EPERM or a reused PID is
   conservatively treated as live; an uncertain process never becomes zero. */
static void fc_reap(fc_shared_t *shared) {
    ngx_uint_t i;
    for (i=0; i<FC_WORKERS; i++) {
        fc_worker_t *worker=&shared->workers[i];
        if (worker->pid && kill(worker->pid,0)==-1 && errno==ESRCH) {
            worker->pid=0; worker->active=0;
        }
    }
}
static void fc_record(fc_conf_t *conf, uint64_t in, uint64_t out) {
    ngx_slab_pool_t *pool;
    fc_bucket_t *b;
    uint64_t tick;
    if (conf->shared == NULL || (in == 0 && out == 0)) { return; }
    tick = (uint64_t) ngx_current_msec / 100;
    pool = (ngx_slab_pool_t *) conf->zone->shm.addr;
    ngx_shmtx_lock(&pool->mutex);
    b = &conf->shared->buckets[tick % FC_BUCKETS];
    if (b->tick != tick) { ngx_memzero(b, sizeof(*b)); b->tick = tick; }
    b->in = fc_add(b->in, in); b->out = fc_add(b->out, out);
    ngx_shmtx_unlock(&pool->mutex);
}
static ngx_int_t fc_zone(ngx_shm_zone_t *zone, void *old_data) {
    fc_conf_t *conf = zone->data, *old = old_data;
    ngx_slab_pool_t *pool = (ngx_slab_pool_t *) zone->shm.addr;
    if (old != NULL) {
        conf->shared = old->shared;
        return conf->shared->magic==FC_MAGIC ? NGX_OK : NGX_ERROR;
    }
    if (zone->shm.exists) {
        conf->shared = pool->data;
        return conf->shared->magic==FC_MAGIC ? NGX_OK : NGX_ERROR;
    }
    conf->shared = ngx_slab_alloc(pool, sizeof(fc_shared_t));
    if (conf->shared == NULL) { return NGX_ERROR; }
    ngx_memzero(conf->shared, sizeof(fc_shared_t));
    conf->shared->magic=FC_MAGIC;
    conf->shared->created = ngx_current_msec;
    conf->shared->creator = ngx_pid;
    pool->data = conf->shared;
    return NGX_OK;
}
static void *fc_create_conf(ngx_conf_t *cf) {
    return ngx_pcalloc(cf->pool, sizeof(fc_conf_t));
}
static char *fc_enable(ngx_conf_t *cf, ngx_command_t *cmd, void *data) {
    fc_conf_t *conf = data;
    ngx_str_t name = ngx_string("frontiercloud_business_traffic");
    (void)cmd;
    if (conf->zone != NULL) { return "is duplicate"; }
    conf->zone = ngx_shared_memory_add(cf, &name, 64*1024, &ngx_http_fc_traffic_module);
    if (conf->zone == NULL) { return NGX_CONF_ERROR; }
    conf->zone->init = fc_zone; conf->zone->data = conf;
    return NGX_CONF_OK;
}
static char *fc_status_conf(ngx_conf_t *cf, ngx_command_t *cmd, void *data) {
    ngx_http_core_loc_conf_t *loc = ngx_http_conf_get_module_loc_conf(cf, ngx_http_core_module);
    (void)cmd; (void)data;
    loc->handler = fc_status;
    return NGX_CONF_OK;
}
static ngx_uint_t fc_prefix(ngx_str_t s, const char *prefix, size_t n) {
    return s.len >= n && ngx_strncmp(s.data, prefix, n) == 0;
}
static ngx_uint_t fc_business(ngx_http_request_t *r) {
    ngx_str_t uri = r->uri;
    if (r->connection->listening != NULL && r->connection->listening->sockaddr->sa_family == AF_UNIX) { return 0; }
    if (fc_prefix(uri, "/health", 7) || fc_prefix(uri, "/metrics", 8)) { return 0; }
    if (r->method == NGX_HTTP_GET && fc_prefix(uri, "/api/v1/media/admin/nodes", 25)) { return 0; }
    if (!fc_prefix(uri, "/internal/v1/", 13)) { return 1; }
    if (fc_prefix(uri, "/internal/v1/media/", 19)) { return (r->method & (NGX_HTTP_GET|NGX_HTTP_HEAD)) != 0; }
    if (fc_prefix(uri, "/internal/v1/storage/", 21)) { return r->method == NGX_HTTP_PUT; }
    if (fc_prefix(uri, "/internal/v1/recordings/", 24) && (r->method & (NGX_HTTP_GET|NGX_HTTP_PUT))) {
        return uri.len < 5 || ngx_strncmp(uri.data+uri.len-5, "/stat", 5) != 0;
    }
    return 0;
}
static fc_request_t *fc_context(ngx_http_request_t *r) {
    ngx_pool_cleanup_t *cl;
    /* Request ctx is cleared on X-Accel internal redirects. Pool cleanup is
       request-owned and survives them without a global identity/path map. */
    for (cl=r->main->pool->cleanup; cl != NULL; cl=cl->next) {
        if (cl->handler == fc_cleanup) { return cl->data; }
    }
    return NULL;
}
static ngx_int_t fc_start(ngx_http_request_t *r) {
    fc_conf_t *conf;
    ngx_pool_cleanup_t *cl;
    fc_request_t *ctx;
    if (r != r->main || !fc_business(r)) { return NGX_DECLINED; }
    conf = ngx_http_get_module_main_conf(r, ngx_http_fc_traffic_module);
    if (conf->shared == NULL) { return NGX_DECLINED; }
    cl = ngx_pool_cleanup_add(r->pool, sizeof(fc_request_t));
    if (cl == NULL) { return NGX_HTTP_INTERNAL_SERVER_ERROR; }
    ctx = cl->data; ngx_memzero(ctx, sizeof(*ctx));
    ctx->request = r; ctx->conf = conf; ctx->sent = r->connection->sent;
    {
        ngx_slab_pool_t *pool=(ngx_slab_pool_t *)conf->zone->shm.addr;
        ngx_shmtx_lock(&pool->mutex);
        if (fc_slot<0 || conf->shared->workers[fc_slot].pid!=ngx_pid ||
            conf->shared->workers[fc_slot].active==UINT64_MAX) {
            conf->shared->incomplete=1;
        } else { conf->shared->workers[fc_slot].active++; ctx->counted=1; }
        ngx_shmtx_unlock(&pool->mutex);
    }
    cl->handler = fc_cleanup;
    ngx_queue_insert_tail(&fc_active, &ctx->link);
    if (!fc_timer.timer_set) { ngx_add_timer(&fc_timer, 100); }
    fc_sample(ctx);
    return NGX_DECLINED;
}
static void fc_sample(fc_request_t *ctx) {
    off_t received = ctx->request->request_length;
    off_t sent = ctx->request->connection->sent;
    uint64_t in = received > ctx->received ? (uint64_t)(received-ctx->received) : 0;
    uint64_t out = sent > ctx->sent ? (uint64_t)(sent-ctx->sent) : 0;
    ctx->received = received; ctx->sent = sent;
    fc_record(ctx->conf, in, out);
}
static void fc_cleanup(void *data) {
    fc_request_t *ctx = data;
    ngx_slab_pool_t *pool=(ngx_slab_pool_t *)ctx->conf->zone->shm.addr;
    fc_sample(ctx);
    ngx_shmtx_lock(&pool->mutex);
    if (ctx->counted) {
        if (fc_slot>=0 && ctx->conf->shared->workers[fc_slot].pid==ngx_pid &&
            ctx->conf->shared->workers[fc_slot].active>0) {
            ctx->conf->shared->workers[fc_slot].active--;
        } else { ctx->conf->shared->incomplete=1; }
        ctx->counted=0;
    }
    ngx_shmtx_unlock(&pool->mutex);
    ngx_queue_remove(&ctx->link);
    if (ngx_queue_empty(&fc_active) && fc_timer.timer_set) { ngx_del_timer(&fc_timer); }
}
static void fc_tick(ngx_event_t *ev) {
    ngx_queue_t *q;
    for (q=ngx_queue_head(&fc_active); q!=ngx_queue_sentinel(&fc_active); q=ngx_queue_next(q)) {
        fc_sample(ngx_queue_data(q, fc_request_t, link));
    }
    if (!ngx_queue_empty(&fc_active)) { ngx_add_timer(ev, 100); }
}
static ngx_int_t fc_body(ngx_http_request_t *r, ngx_chain_t *in) {
    ngx_int_t rc = fc_next_body(r, in);
    fc_request_t *ctx = fc_context(r);
    if (ctx != NULL) { fc_sample(ctx); }
    return rc;
}
static ngx_int_t fc_input(ngx_http_request_t *r, ngx_chain_t *in) {
    ngx_int_t rc = fc_next_input(r, in);
    fc_request_t *ctx = fc_context(r);
    if (ctx != NULL) { fc_sample(ctx); }
    return rc;
}
static ngx_int_t fc_status(ngx_http_request_t *r) {
    fc_conf_t *conf = ngx_http_get_module_main_conf(r, ngx_http_fc_traffic_module);
    ngx_slab_pool_t *pool;
    uint64_t tick, in=0, out=0, recent=0, age, active=0;
    ngx_uint_t i;
    ngx_buf_t *buf;
    ngx_chain_t chain;
    ngx_msec_t created;
    ngx_pid_t creator;
    ngx_time_t *now;
    ngx_int_t rc;
    if (r->connection->listening == NULL || r->connection->listening->sockaddr->sa_family != AF_UNIX) { return NGX_HTTP_FORBIDDEN; }
    if (r->method != NGX_HTTP_GET) { return NGX_HTTP_NOT_ALLOWED; }
    if (conf->shared == NULL) { return NGX_HTTP_SERVICE_UNAVAILABLE; }
    tick = (uint64_t)ngx_current_msec/100;
    pool=(ngx_slab_pool_t *)conf->zone->shm.addr;
    ngx_shmtx_lock(&pool->mutex);
    fc_reap(conf->shared);
    if (conf->shared->incomplete) {
        ngx_shmtx_unlock(&pool->mutex); return NGX_HTTP_SERVICE_UNAVAILABLE;
    }
    for(i=0;i<FC_WORKERS;i++) { active=fc_add(active,conf->shared->workers[i].active); }
    created=conf->shared->created; creator=conf->shared->creator;
    for(i=0;i<FC_BUCKETS;i++) {
        fc_bucket_t *b=&conf->shared->buckets[i];
        if(b->tick>tick) { continue; }
        age=tick-b->tick;
        if(age>=FC_BUCKETS) { continue; }
        in=fc_add(in,b->in); out=fc_add(out,b->out);
        if(age<10) { recent=fc_add(recent,fc_add(b->in,b->out)); }
    }
    ngx_shmtx_unlock(&pool->mutex);
    buf=ngx_create_temp_buf(r->pool,512);
    if(buf==NULL) { return NGX_HTTP_INTERNAL_SERVER_ERROR; }
    now=ngx_timeofday();
    buf->last=ngx_sprintf(buf->pos,"{\"format\":\"frontiercloud-nginx-traffic\",\"version\":2,\"epoch\":\"%P-%M\",\"observed_at_ms\":%uL,\"ingress_bytes_per_second\":%uL,\"egress_bytes_per_second\":%uL,\"recent_bytes_per_second\":%uL,\"active_business_requests\":%uL}\n",creator,created,(uint64_t)now->sec*1000+now->msec,in/5,out/5,recent,active);
    r->headers_out.status=NGX_HTTP_OK;
    r->headers_out.content_length_n=buf->last-buf->pos;
    ngx_str_set(&r->headers_out.content_type,"application/json");
    rc=ngx_http_send_header(r);
    if(rc==NGX_ERROR || rc>NGX_OK || r->header_only) { return rc; }
    buf->last_buf=1; chain.buf=buf; chain.next=NULL;
    return ngx_http_output_filter(r,&chain);
}
static ngx_int_t fc_init(ngx_conf_t *cf) {
    ngx_http_core_main_conf_t *core=ngx_http_conf_get_module_main_conf(cf,ngx_http_core_module);
    ngx_http_handler_pt *handler=ngx_array_push(&core->phases[NGX_HTTP_POST_READ_PHASE].handlers);
    if(handler==NULL) { return NGX_ERROR; }
    *handler=fc_start;
    fc_next_body=ngx_http_top_body_filter; ngx_http_top_body_filter=fc_body;
    fc_next_input=ngx_http_top_request_body_filter; ngx_http_top_request_body_filter=fc_input;
    return NGX_OK;
}
static ngx_int_t fc_process(ngx_cycle_t *cycle) {
    ngx_slab_pool_t *pool;
    ngx_uint_t i;
    ngx_queue_init(&fc_active); ngx_memzero(&fc_timer,sizeof(fc_timer));
    fc_timer.handler=fc_tick; fc_timer.log=cycle->log;
    fc_worker_conf=ngx_http_cycle_get_module_main_conf(cycle,ngx_http_fc_traffic_module);
    if (fc_worker_conf==NULL || fc_worker_conf->shared==NULL) { return NGX_OK; }
    pool=(ngx_slab_pool_t *)fc_worker_conf->zone->shm.addr;
    ngx_shmtx_lock(&pool->mutex); fc_reap(fc_worker_conf->shared);
    for(i=0;i<FC_WORKERS;i++) {
        if (fc_worker_conf->shared->workers[i].pid==0 || fc_worker_conf->shared->workers[i].pid==ngx_pid) {
            fc_slot=(ngx_int_t)i; fc_worker_conf->shared->workers[i].pid=ngx_pid;
            fc_worker_conf->shared->workers[i].active=0; break;
        }
    }
    if(fc_slot<0) { fc_worker_conf->shared->incomplete=1; }
    ngx_shmtx_unlock(&pool->mutex);
    return NGX_OK;
}
static void fc_exit(ngx_cycle_t *cycle) {
    ngx_slab_pool_t *pool;
    (void)cycle;
    if(fc_timer.timer_set) { ngx_del_timer(&fc_timer); }
    if(fc_worker_conf!=NULL && fc_worker_conf->shared!=NULL && fc_slot>=0) {
        pool=(ngx_slab_pool_t *)fc_worker_conf->zone->shm.addr;
        ngx_shmtx_lock(&pool->mutex);
        if(fc_worker_conf->shared->workers[fc_slot].pid==ngx_pid) {
            fc_worker_conf->shared->workers[fc_slot].pid=0;
            fc_worker_conf->shared->workers[fc_slot].active=0;
        }
        ngx_shmtx_unlock(&pool->mutex);
    }
}
