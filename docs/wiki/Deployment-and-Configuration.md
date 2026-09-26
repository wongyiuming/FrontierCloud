# 部署与配置

## 1. 前置条件

推荐环境：

- Linux 主机；
- Docker Engine；
- Docker Compose v2；
- 正确的 DNS 与证书条件（当启用 HTTPS 时）；
- 足够的磁盘空间保存 `./data`、MySQL、Redis、镜像和备份。

## 2. 最小启动

HTTP 本地测试不要求 `.env`：

```bash
docker compose up -d --build --wait
```

查看状态：

```bash
docker compose ps
```

查看日志：

```bash
docker compose logs -f --tail=200
```

## 3. HTTPS

生产 Master/Follower 固定角色应使用 HTTPS。

创建 `.env`：

```dotenv
TLS_ENABLED=true
SERVER_NAME=media.example.com
```

默认部署读取：

```text
certs/fullchain.pem
certs/privkey.pem
```

也可以通过环境变量调整：

```dotenv
SSL_CERT_PATH=./certs/fullchain.pem
SSL_KEY_PATH=./certs/privkey.pem
```

证书必须与 `SERVER_NAME` 匹配。

## 4. 主要 Compose 服务

```text
secrets-init
media-init
updater
web
redis
mysql
nginx
stun
```

### secrets-init

首次启动生成并维护运行期 secrets，例如：

- MySQL application password；
- MySQL root password；
- Admin Key；
- metrics token。

Secret volume 为：

```text
runtime_secrets
```

### media-init

初始化宿主机媒体目录与权限。

主要数据根目录：

```text
./data
```

### web

FastAPI 主应用。默认以非 root UID 运行，容器根文件系统为只读，只挂载必要的可写卷。

### updater

负责版本构建、容器替换、升级/回滚和集群发布控制。

### mysql / redis

分别保存业务事实和运行期缓存。

### nginx

公网入口，负责 TLS、反代、静态资源以及边缘控制。

### stun

提供 WebRTC 网络观测所需 STUN 服务。

## 5. 数据卷与持久化

重点数据：

```text
./data                 媒体、歌词、录音等文件
mysql_data             MySQL 数据
redis_data             Redis 数据
runtime_secrets        Admin/MySQL/metrics secrets
updater_control        Web 与 updater 控制通道
maintenance_state      维护模式状态
```

普通：

```bash
docker compose down
```

不会删除 volume。

危险操作：

```bash
docker compose down --volumes
```

会删除数据库和 secrets 等持久数据，不应用于正常升级。

## 6. 常用环境变量

完整清单以 `.env.example` 为准。常用项：

```dotenv
TLS_ENABLED=true
SERVER_NAME=media.example.com
INSTANCE_NAME=frontiercloud
LOG_LEVEL=INFO
LOG_FORMAT=json
MYSQL_DATABASE=office_automation
MYSQL_USER=media_admin
WEBRTC_STUN_PORT=3478
PUBLIC_BIND_ADDRESS=0.0.0.0
```

### GitHub 发布验证 Token

发布验证支持可选只读 GitHub Token：

```dotenv
GITHUB_API_TOKEN=...
```

用途：

- 提高 GitHub REST API 请求额度；
- 避免匿名 60 requests/hour 限流；
- Token 只在 Master 服务端使用。

即使没有 Token，程序也有缓存和 rate-limit backoff，但生产环境建议配置最小权限只读 Token。

## 7. Secrets 查看

查看 Admin Key：

```bash
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/admin_key'
```

查看 metrics token：

```bash
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/metrics_token'
```

查看 MySQL 应用密码：

```bash
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/mysql_password'
```

不要把输出写入公共日志、Issue、PR 或截图。

## 8. 健康检查

Liveness：

```text
/health/live
```

Readiness：

```text
/health/ready
/health
```

Metrics：

```text
/metrics
```

`/metrics` 需要生成的 Bearer token。

## 9. 日志

容器统一输出 stdout/stderr，并通过 Docker json-file logging 做大小和文件数限制。

查看单服务：

```bash
docker compose logs -f web
docker compose logs -f updater
docker compose logs -f mysql
docker compose logs -f nginx
```

建议生产环境由外部日志平台采集。FrontierCloud 当前不会自动部署 Loki/ELK 等平台。

## 10. IPv4 / IPv6

公网端口默认绑定：

```dotenv
PUBLIC_BIND_ADDRESS=0.0.0.0
```

只有宿主机和网络已经准备好 IPv6 时才设置：

```dotenv
PUBLIC_BIND_ADDRESS=::
```

不要仅为了“支持 IPv6”盲目修改；实际绑定行为由部署环境决定。

## 11. 初次部署建议步骤

```text
1. clone / checkout 正式 main
2. 准备 .env
3. 准备 HTTPS 证书（生产）
4. docker compose up -d --build --wait
5. 检查 /health/ready
6. 读取 Admin Key
7. 登录 Admin
8. 固定 Master/Follower 角色
9. 配对 Follower
10. 配置 Storage / Compute / Backup
11. 验证 Desired == Observed
12. 验证媒体上传、播放与节点状态
```

## 12. 升级前不要做什么

不要：

- 手工删除 MySQL 表；
- 手工修改 Schema Generation；
- 直接删除 managed media 文件；
- 在 pending delete/recovery 状态时绕过程序删目录；
- 为了“升级”执行 `docker compose down --volumes`；
- 在正式集群中直接把 Follower 当第二个独立 Master 使用。

正式升级走系统版本管理页和 release gate。
