# 本次增量修改说明

## 新增/修改文件

- `app/api/v1/admin.py`：Admin 提权、Session、逐文件上传、下载、删除、隐藏、安全日志、退出。
- `app/services/admin_service.py`：15 分钟滑动临时 Token、Redis Session、MySQL 历史记录、审计。
- `app/services/media_manager.py`：文件树、上传校验、批量文件操作、ZIP 下载、隐藏状态。
- `app/core/db.py`：MySQL 初始化表结构。
- `app/core/config.py`：Admin/MySQL 配置。
- `app/api/v1/media.py`：公共索引与播放增加隐藏状态和根目录媒体禁止规则。
- `app/services/media_catalog_cache.py`：Redis 分类/曲目缓存、版本化主动失效与故障降级。
- `app/core/client_ip.py`、`app/middleware/ip_security.py`：可信 Nginx 单一真实 IP、非法 API 识别与请求封禁。
- `app/services/ip_security.py`：Redis 滑动窗口/24 小时封禁、MySQL 审计与永久白名单。
- `main.py`：保留旧 `[LOG]`，增加 `[REQUEST]` 中文 Query 渲染日志，并运行周期性临时 Admin Token 签发任务。
- `static/media/index.html`：增加“提权”。
- `static/media/admin.html`、`static/css/admin.css`、`static/js/admin.js`：Admin 文件管理、双上传进度条与安全日志 UI。
- `docker-compose.yaml`：新增 MySQL、服务 healthcheck 与 10 MiB × 3 的容器日志轮转。
- `nginx/nginx.conf`：Admin 上传代理、真实 IP 固定为 Nginx remote address、上传超时与 no-store。
- `pyproject.toml`：增加 SQLAlchemy + asyncmy。

## 临时 Token

Web 容器启动时会生成一枚 32-byte URL-safe Token，之后固定每 15 分钟再生成一枚。待领取 Token 在交接时保留 5 秒重叠，避免调度抖动产生真空；成功使用后，每枚 Token 都有独立的 15 分钟滑动 TTL。新 Token 不覆盖旧 Token，持续被 Admin Session 使用的旧 Token 会独立续期，因此可以比后生成但未使用的 Token 存活更久。MySQL 只保存 SHA-256 摘要；明文只输出到 web 容器 stdout，因此宿主机可以使用 `docker logs -f office_automation_web` 获取。

也保留了 `/api/v1/media/admin/token/issue`，需要 `X-Token: ADMIN_BOOTSTRAP_TOKEN` 才能重新签发。

生产环境必须修改 `.env` 中所有 `change_me` / `change_*` 默认值，并保持 `ADMIN_COOKIE_SECURE=true`。

## 重要行为

- `data/media/xxx.mp3` 永远禁止通过 Admin 上传，公共 `/stream` 也拒绝直接读取。
- 上传只允许当前播放器支持的音频/视频扩展，并进行文件头校验。
- 公共视图隐藏目录完全不可见，Admin 仍然可见。
- Admin 文件树按目录懒加载，不一次性递归返回整个媒体库。
- Admin Session 和临时 Token 都是 15 分钟滑动 TTL。
- 多文件与文件夹任务逐文件上传（默认单任务最多 5000 个），分别显示当前文件和整个任务进度；单文件上限为 800 MiB，Nginx 请求上限保留 multipart 余量为 820 MiB。
- 公共分类和曲目列表默认在 Redis 缓存 300 秒；Admin 上传、删除、隐藏或恢复后立即切换缓存版本。
- Admin 播放与移动功能已移除；日志区只镜像当前 Web 进程日志，不挂载高权限 Docker Socket。
- 所有管理修改接口要求 HttpOnly Session Cookie + CSRF Header。
- 未匹配路由或错误 HTTP 方法在 1 小时内第 6 次触发 24 小时自动封禁；合法路由内部返回的 404 不计数。
- Admin 安全小窗口展示合法 API 数、最近 24 小时封禁和永久白名单，并支持单次解封、加白及移出白名单。
