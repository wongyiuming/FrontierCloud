# 运维与排障

## 1. 排障顺序

不要一上来就重启全部容器。推荐按层判断：

```text
1. 业务是否真的不可用
2. Nginx / Web readiness
3. MySQL / Redis
4. 节点关系和心跳
5. Storage / Compute / Backup 各自状态
6. Updater / 发布状态
7. GitHub 等外部依赖
```

FrontierCloud 的很多状态彼此独立。例如 GitHub 发布验证失败不代表媒体业务失败；Backup 失败也不代表播放失败。

## 2. 快速健康检查

```bash
docker compose ps
```

```bash
curl -fsS http://127.0.0.1/health/live
curl -fsS http://127.0.0.1/health/ready
```

HTTPS 部署按实际域名访问。

查看最近日志：

```bash
docker compose logs --tail=200 web
docker compose logs --tail=200 nginx
docker compose logs --tail=200 updater
docker compose logs --tail=200 mysql
docker compose logs --tail=200 redis
```

## 3. 业务正常但发布页报错

先区分：

```text
业务数据平面故障
```

和：

```text
发布验证外部依赖故障
```

例如 GitHub REST API 受到限流时：

- 当前 Master/Follower 可以继续提供业务；
- 已运行版本不受影响；
- 新升级必须 fail closed；
- UI 会显示 GitHub 验证受限；
- Updater 本身可能仍然 ready。

### GitHub API 限流检查

```bash
docker compose exec -T web python - <<'PY'
import httpx
import time

url = "https://api.github.com/repos/wongyiuming/FrontierCloud/branches/main"
with httpx.Client(
    trust_env=False,
    timeout=httpx.Timeout(5, connect=3),
    headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "FrontierCloud-release-control",
    },
) as c:
    r = c.get(url)

print("HTTP:", r.status_code)
for name in (
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-used",
    "x-ratelimit-reset",
):
    print(name + ":", r.headers.get(name))
reset = r.headers.get("x-ratelimit-reset")
if reset:
    print("reset in:", max(0, int(reset) - int(time.time())), "seconds")
print(r.text[:1000])
PY
```

如果：

```text
HTTP 403
x-ratelimit-remaining: 0
```

就是 GitHub API quota 耗尽，不是 github.com 网页网络不通。

生产 Master 建议配置只读：

```dotenv
GITHUB_API_TOKEN=...
```

## 4. 节点 Offline

检查：

1. `last_heartbeat`；
2. RTT；
3. failures；
4. HTTPS 证书；
5. peer endpoint；
6. Follower Web 是否 healthy；
7. 节点间 DNS/路由/防火墙。

不要因为一次 RTT 变高就判断节点离线。更重要的是心跳新鲜度和连续失败。

## 5. 配置开关已打开但没有生效

看 Desired / Observed。

可能原因：

- Follower 离线；
- 下一轮心跳尚未到达；
- Follower 拒绝配置；
- 版本不一致；
- TLS/关系状态异常。

UI 中“配置已保存”不等于“Follower 已生效”。

## 6. Storage 排障

重点看：

```text
storage_enabled
health
writable
allocated_bytes
used_bytes
reserved_bytes
physical_free_bytes
```

### 显示有逻辑空间但无法上传

可能是：

- physical free 不足；
- reserved bytes 已占用；
- 节点 health 异常；
- writable=false；
- 上传过程中节点状态改变。

### Catalog 与磁盘占用差异

`SUM(global_media_objects.size_bytes)` 与 Storage Member 的 `used_bytes` 不要求严格相等。

如果差异异常大，再检查：

- 临时上传；
- pending delete；
- recovery 数据；
- 人工放入文件；
- 未清理残留。

## 7. Compute 排障

重点看：

- `worker_slots`；
- running；
- queued；
- available slots；
- CPU；
- available memory；
- capability；
- `attempts`；
- lease expiration。

### 为什么任务没分给节点

检查：

1. Compute 是否 enabled 且已生效；
2. capability 是否匹配；
3. 是否还有 slot；
4. 任务是否 pinned 给其他 member；
5. 是否有其他节点先 lease；
6. task 是否已失败/完成。

当前普通任务主要是 capability + FIFO，不是按 CPU 最低自动择优。

## 8. Backup 排障

重点看：

- Enabled / Effective；
- `last_success`；
- 最近尝试状态；
- generation；
- checksum；
- 恢复点数量；
- 下次计划时间。

### `pending` 不代表永远没有成功备份

主判断应看 durable `last_success` 和最近恢复点，而不是只看一个瞬时 state。

### 备份失败后

新版逻辑会采用较短失败重试，不需要等完整正常备份周期后才再次尝试。

## 9. MySQL 启动失败 / Schema 问题

常见情况：

### 数据库 Generation 低于应用

应用应自动逐代 migration。

### migration 失败

应用拒绝继续启动，generation marker 不推进。检查异常原因，修复后重新启动允许幂等重试。

### 数据库 Generation 高于应用

说明运行了旧版本应用读取新版本数据库。系统应拒绝启动，禁止自动 downgrade。

### 非空数据库没有 schema marker

不会自动猜版本进行 ALTER。需要人工确认来源，而不是绕过保护。

## 10. Updater 卡住

检查：

```bash
docker compose logs --tail=300 updater
```

再看发布页：

```text
state
phase
target_sha
current_sha
previous_sha
detail
```

如果处于 maintenance，先确认发布流程是否仍在运行，不要直接删除 maintenance volume。

## 11. 集群版本不一致

例如：

```text
Master A
Follower B
Follower C
```

不能简单把“Master success”理解为全体升级完成。

需要确认每个 Follower：

```text
current_sha == target_sha
state == success/idle compatible state
reachable == true
release_branch == main
```

发生部分节点升级失败时，维护模式是安全边界。不要为了恢复页面访问而先强制关闭维护，再慢慢查版本。

## 12. 删除/恢复异常

FrontierCloud 的删除有数据库 journal 和恢复语义。

不要：

```bash
rm -rf data/...
```

来“解决” pending delete/recovery。

如果 recovery 阻塞业务变更，先：

- 查看 Web 日志；
- 查看相关数据库状态；
- 确认 MySQL 与 data 是否来自同一个恢复点；
- 必要时恢复成一致的数据库+文件+secrets 备份组合。

## 13. 备份策略

生产至少备份：

```text
MySQL
./data
runtime_secrets
```

它们应被视为一组恢复资产。

只恢复数据库但不恢复对应文件，或只恢复文件不恢复数据库，都可能制造 Catalog/文件系统漂移。

## 14. 推荐事故信息采集

在真正改配置前先保存：

```bash
date -Is
docker compose ps
docker compose logs --tail=300 web > /tmp/frontier-web.log
docker compose logs --tail=300 updater > /tmp/frontier-updater.log
docker compose logs --tail=300 nginx > /tmp/frontier-nginx.log
```

数据库问题再附加：

```bash
docker compose exec -T mysql sh -lc '
MYSQL_PWD="$(cat /run/frontiercloud-secrets/mysql_password)";
export MYSQL_PWD;
mysql -u "$MYSQL_USER" "$MYSQL_DATABASE" -e "SELECT * FROM frontiercloud_schema; SELECT * FROM frontiercloud_schema_migrations ORDER BY generation;"
'
```

先保留现场，再执行修复。
