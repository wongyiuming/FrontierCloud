listen {
  port = 4040
}

namespace "frontiercloud_nginx" {
  source = {
    files = ["/mnt/nginxlogs/access_log.log"]
  }

  format = "$remote_addr - $remote_user [$time_local] \"$request\" $status $body_bytes_sent \"$http_referer\" \"$http_user_agent\" rt=$request_time urt=$upstream_response_time"

  labels {
    environment = "production"
  }

  histogram_buckets = [0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10]
}
