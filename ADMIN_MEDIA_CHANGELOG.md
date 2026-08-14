# 本次增量修改说明

## 新增/修改文件

- `app/api/v1/admin.py`：Admin 提权、Session、上传、下载、删除、移动、隐藏、退出。
- `app/services/admin_service.py`：15 分钟滑动临时 Token、Redis Session、MySQL 历史记录、审计。
- `app/services/media_manager.py`：文件树、上传校验、批量文件操作、ZIP 下载、隐藏状态。
- `app/core/db.py`：MySQL 初始化表结构。
- `app/core/config.py`：Admin/MySQL 配置。
- `app/api/v1/media.py`：公共索引与播放增加隐藏状态和根目录媒体禁止规则。
- `main.py`：保留旧 `[LOG]`，增加 `[REQUEST]` 中文 Query 渲染日志，并启动时生成临时 Admin Token。
- `static/media/index.html`：增加“提权”。
- `static/media/admin.html`、`static/css/admin.css`、`static/js/admin.js`：完整 Admin UI。
- `docker-compose.yaml`：新增 MySQL，Redis/MySQL healthcheck。
- `nginx/nginx.conf`：Admin 上传代理、真实 IP 固定为 Nginx remote address、上传超时与 no-store。
- `pyproject.toml`：增加 SQLAlchemy + asyncmy。

## 临时 Token

Web 容器启动后会自动生成 32-byte URL-safe Token。未使用的 Token 默认有 24 小时领取窗口；首次提权成功后切换为 15 分钟滑动 TTL。MySQL 只保存 SHA-256 摘要；明文只输出到 web 容器 stdout，因此宿主机可以使用 `docker logs -f office_automation_web` 获取。

也保留了 `/api/v1/media/admin/token/issue`，需要 `X-Token: WALL_ADMIN_TOKEN` 才能重新签发。

生产环境必须修改 `.env` 中所有 `change_me` / `change_*` 默认值，并保持 `ADMIN_COOKIE_SECURE=true`。

## 重要行为

- `data/media/xxx.mp3` 永远禁止通过 Admin 上传，公共 `/stream` 也拒绝直接读取。
- 上传只允许当前播放器支持的音频/视频扩展，并进行文件头校验。
- 公共视图隐藏目录完全不可见，Admin 仍然可见。
- Admin 文件树按目录懒加载，不一次性递归返回整个媒体库。
- Admin Session 和临时 Token 都是 15 分钟滑动 TTL。
- 浏览器会按 96 MiB/200 个文件自动拆分文件及文件夹上传请求，Nginx 单请求上限为 128 MiB。
- 所有管理修改接口要求 HttpOnly Session Cookie + CSRF Header。
