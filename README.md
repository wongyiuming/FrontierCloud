# Office Automation API

# TODO

- 🚧 **播放积分制，播放越多积分越高，积分高则排名在最后(新鲜感很重要再好听也顶不住一直听)，新增"喜欢"按钮，可以升级和降级，同级别随机排序，喜欢的级别越高排名越靠前，后续可能或引入登录，把喜好和积分绑定到个人，所以需要后续方便扩展，暂时不需要，因为用户较少**
- 🚧 **修复潜在的安全问题，使用admin-token保护docs redoc openapi**
- 🚧 **新增需求webRTC,探测用户原始公网ip,并记录到日志，完成后加上现有的 两者日志类型 log和 request (我在日志里面看到的)现在一次访问理论上有3条日志，webRTC的ip,ng记录到的ip(可能是用户的落地ip也可能与webRTC结果相同, 相同则说明用户没有做安全防护，或者他禁用了安全防护，后者说明他是老资历)**
- 🚧 **当前播放页最底部的进度条非常难以选中，把他从线条扩展为有面积的矩形**
- 🚧 **bug修复，未知的原因尝试使用过期token(曾经可用)，前端仅提示提权失败太泛，之前要求的token过期在某次提交中又失效了，另外我查看mysql里面居然没有黑名单完整的记录，虽然我们的策略是仅封24小时，但我们必须记录完整的曾经攻击我们的全部ip名单，一来审计方便，二来用这份数据对后续的安全加固提供参考价值且我们可以重新封禁他们**
- 🚧 **新增grafana 普罗米修斯 等监控组件，监控web ng redisw mysql 实时状态和备份历史状态数据(默认策略是24小时内可查，空间有限暂定24小时)**
- 🚧 **新增异常上报邮箱和tg的逻辑，取值自监控组件发生异常进行上报，比如健检接口连续30秒超时，带宽异常，cpu并发上去90%后持续不降低，mem占用率高位90%持续不降低，df -h 剩余空间不足8%(目前的8%即1.6G)**
- 🚧 **聊天墙功能完全重构，每次进入前端页面会让选择再池子里面点击选择一个头像作为本次头像有效期15分钟，与当前pc环境绑定，拉取一些没有喜好特征的二次元头像避免被记录画像习惯，比如可以用懒洋洋企鹅等**
- 🚧 **聊天墙功能完全重构，现在要实现真正的阅后即焚功能，只能通过两个手指同时按住屏幕查看明文，没按之前能查看有一条消息，但不能知道消息具体内容**
- 🚧 **聊天墙功能完全重构，现在可以输入文件，图片，表情，表情支持同样表情库即可，不支持视频和尺寸超过20m的图片，整体资源有限**

# FrontierCloud 周末升级 TODO

## 今晚范围、部署拓扑与执行纪律

* [ ] 今晚必须完成：播放排序与喜好、播放器进度条可用性、API 文档认证、管理员 Token 过期诊断、封禁历史永久审计、WebRTC 客户端公网地址观察、核心监控与 24 小时历史指标。
* [x] 匿名聊天墙已经纳入本轮范围：使用浏览器端 AES-GCM 加密、进程内一次性 Message Key、15 分钟匿名头像会话，以及“先选择消息、再双指持续按住”的移动端揭示流程。
* [ ] 完全放弃 Email 告警；Telegram Bot 是唯一告警渠道并升级为今晚 P0 必须项。生产部署前必须完成真实告警与恢复通知的端到端测试。
* [ ] 生产 Web Compose 部署到 `production-web`；Prometheus、Grafana、Alertmanager、Blackbox Exporter 与历史指标存储部署到独立的固定公网 `monitoring-vps`。家庭 CentOS 环境不再参与部署。
* [ ] `monitoring-vps` 当前资源预算为 2 CPU、约 2GB RAM、约 35GB Disk；所有监控容器必须设置内存上限，Prometheus 同时设置 24 小时保留与容量上限，所有容器日志必须轮转。
* [ ] 不使用 SSH 隧道。Web 主机上的 Exporter 只加入内部 Docker 网络，由现有 Nginx 通过 HTTPS 的受保护内部指标路径转发；Nginx 和主机防火墙仅允许 `monitoring-vps` 的固定公网 IP 访问这些路径。
* [x] Exporter 容器不得单独发布公网端口；RN 只公开 HTTPS 网关的 `80/443`。Grafana 使用自身账号系统，Prometheus 与 Alertmanager 使用各自原生 Basic Auth，三个后端端口均仅暴露在 Docker 网络。
* [ ] 所有密码、Bot Token、Chat ID、Exporter 凭据和环境相关 IP（包括监控端白名单 IP）仅放在目标主机的 `.env`/受限凭据文件中；仓库只提交无秘密的 `.env.example`。代码、Compose、Nginx 模板不得硬编码任何具体部署 IP；`.env` 必须为 `0600` 且永不加入 Git。
* [ ] 任何曾经出现在聊天、日志或截图里的 Bot Token 都视为已泄露，部署前必须轮换；不得为了赶进度继续使用已暴露 Token。
* [ ] 开始修改前记录本地、远程仓库和当前生产 commit SHA；生产发布始终使用确定的 SHA，不直接部署不确定的工作区状态。
* [ ] 每个提交最多包含一个功能性变化；实现、对应测试和该功能所需文档可以属于同一提交。一个提交不得混入第二个功能，也不得混入无关格式化。
* [ ] 允许大量本地原子提交；全部必须项、本地测试和镜像构建完成后再统一推送，目标是整晚只推送一次。
* [ ] 推送后若部署失败，先把生产切回已记录的上一可用 SHA/image/database 状态，再修复、测试并创建新的原子提交；禁止在共享 `main` 上强制改写已推送历史。

## 第一批：播放排序与喜好系统

* [ ] 新增播放积分机制。媒体被有效播放后增加播放积分，积分越高代表近期重复播放越多，在相同喜好等级下排序越靠后，以保持内容新鲜感。
* [ ] “有效播放”默认定义为：媒体实际处于播放状态的累计时间达到 `max(5 秒, min(30 秒, 媒体时长 × 50%))`；暂停时间、预加载、拖动跳转和页面刷新本身不计入。同一 `playback_session_id + media_id` 最多增加 1 分。
* [ ] 浏览器每次进入播放页生成随机 `playback_session_id`；服务端设置短 TTL 并做幂等校验，客户端重复上报不得重复计分。
* [ ] 新增喜好等级的升级和降级操作。今晚默认使用 `-2..+2` 五级、默认 `0`；前端提供明确的升/降级控件，不把一次普通播放隐式视为“喜欢”。
* [ ] 排序优先级暂定为：`喜好等级 DESC → 播放积分 ASC → 同条件随机排序`。
* [ ] 同等级随机排序应在单次播放会话内保持稳定，避免分页过程中出现重复或遗漏；重新进入播放页面后可重新生成随机顺序。
* [ ] 媒体基础信息与播放统计/喜好数据分离设计，避免将个人偏好直接固化到媒体表。今晚播放积分为累计有效播放次数，不做时间衰减。
* [ ] 当前用户数量较少，暂不实现登录和个人喜好；数据模型/API 预留未来 `user_id / profile_id` 维度，使后续可以把播放积分和喜好绑定个人账户。
* [ ] 增加数据库 migration、排序单元测试、重复播放计分测试以及旧数据兼容测试。
* [ ] 补充说明下，现在已有 单击下一曲，双击上一曲 的逻辑，且映射范围是整个网页窗口，那么现在把进度条从一根线放大到有面积的矩形，需要注意不能让任何一个坐标同时出现点击下一曲和跳转进度条的功能映射
* [ ] 进度条扩大的是可点击/触摸命中区域，不夸大已经播放的视觉比例；鼠标、触摸和键盘拖动均可用。
* [ ] 进度条、播放器底栏、设置、侧栏等控件区域必须在捕获阶段排除于“单击下一曲/双击上一曲”手势；用自动化测试验证同一坐标不会同时触发 seek 与切歌。

## 第二批：API 文档安全

* [ ] `/docs`、`/redoc`、`/openapi.json` 全部加入管理员认证，禁止只保护 Swagger 页面而继续公开 OpenAPI Schema。
* [ ] 优先复用 FrontierCloud 现有管理员提权/session 鉴权体系，避免为了文档页面新增另一套长期凭据。
* [ ] Admin Token 不允许通过 URL Query 参数传递。
* [ ] 验证未提权用户无法直接读取 `/openapi.json`。
* [ ] 验证管理员正常打开 Swagger/ReDoc，并确保页面内部读取 OpenAPI Schema 时能够继续通过认证。
* [ ] 验证管理员 Session 失效以后 Docs/OpenAPI 同步失效。

## 第三批：管理员凭据诊断与永久封禁审计

* [ ] 先用自动化测试复现“曾经可用的 Token 被提交后仅显示泛化提权失败”，再修复；不得在没有证据时把问题归因于 Redis、前端或计时器。
* [ ] 服务端对“格式错误/从未签发”“已签发但已过期”“尝试过于频繁”返回可区分但不泄露秘密的状态；前端显示明确中文提示，并保留速率限制。
* [ ] 验证 Token、管理员 Session 和滑动 TTL 的预期关系；Token 过期后依赖它的 Session 同步失效，新的独立 Token 不得意外撤销仍有效的旧 Token。
* [ ] `ip_auto_ban_events` 改为永久审计事件表：24 小时只决定自动封禁有效期，不再删除过期历史记录。
* [ ] 删除运行时和查询路径中清理历史封禁事件的 `DELETE`；只更新事件状态为 `expired/unbanned/whitelisted`。
* [ ] 管理端默认仍展示近期记录，但支持按 IP、状态和时间分页查询全部历史，避免一次加载无限数据。
* [ ] 支持管理员从历史事件显式重新封禁 IP；重新封禁必须创建新的审计事件，记录操作者 Session Hash、原因、开始与结束时间，不修改旧事件伪造历史。
* [ ] 对现有 MySQL 数据先备份再迁移；确认当前仍存在的历史记录不丢失，并增加查询索引和回归测试。

## 第四批：WebRTC 客户端公网地址观察

* [x] 浏览器通过 `.env` 配置的自建 STUN-only coturn 收集 `srflx` ICE Candidate；公网主机与端口不得硬编码到代码或 Compose，coturn 设置独立资源上限且不启用 TURN 中继。
* [ ] 不申请摄像头或麦克风权限；收集完成或超时后立即关闭 `RTCPeerConnection`，不建立媒体会话。
* [ ] 客户端仅上报规范化后的少量 IPv4/IPv6 候选地址；服务端校验格式、去重、限长、限频，不接受客户端声称其地址“可信”。
* [x] 每个 HTTP 请求只输出一条 `[REQUEST]`；WebRTC 上报请求在同一行同时记录 `REAL_IP`、`PROXY_IP`、`WEBRTC_IP`、匹配结果与采集结果，不再重复输出 `[LOG]`/`[WEBRTC_IP]`。
* [x] WebRTC 地址仅与承载该 payload 的上报请求关联；普通请求显示 `WEBRTC_IP: -`，不得按共享出口 IP 推断到其他请求，也不得把相同/不同解释成用户资历、代理类型或安全水平。
* [ ] WebRTC 被浏览器、扩展或网络策略禁用时只记录可诊断的失败原因，不影响播放器和其他业务功能。
* [ ] 增加 IPv4、IPv6、多个 Candidate、伪造 payload、超时、禁用 WebRTC 与日志注入回归测试；地址不进入 Prometheus Label。

## 第五批：监控、历史指标与异常通知

* [ ] 在 `monitoring-vps` 部署 Prometheus，用于保存监控指标；设置 `24h` 时间保留和不超过 `6GB` 的容量保留，任一限制先达到即清理，避免 35GB 宿主机空间被指标写满。
* [ ] 部署 Grafana，用于展示实时和历史状态。
* [ ] 增加宿主机 CPU、Memory、Filesystem、Network 指标。
* [ ] 增加 Docker 容器 CPU、Memory、Restart、Up/Down 等指标。
* [ ] 增加 Nginx 请求量、状态码、连接数和响应时间指标。
* [ ] 增加 FastAPI `/metrics` 或等价应用指标，包括请求数、延迟、状态码、异常和健康状态。
* [ ] 增加 Redis 内存、连接、命令量、eviction、可用性等指标。
* [ ] 增加 MySQL 连接、查询、错误及可用性等指标。
* [ ] `monitoring-vps` 部署 Prometheus、Grafana、Alertmanager、Blackbox Exporter；Web 主机部署 Node Exporter、cAdvisor、Redis Exporter、MySQL Exporter 和所需的 Nginx 指标采集器。
* [ ] Exporter 使用最小权限账号；MySQL Exporter 使用只读监控用户，Redis/MySQL 密码不出现在命令行、镜像层、日志或 Prometheus Label 中。
* [ ] Nginx 连接/请求量可使用受限 `stub_status`，状态码和响应时间使用访问日志 Exporter 或等价可验证方案；不得把 FastAPI 指标冒充为 Nginx 指标。
* [ ] 内部指标路径采用双重限制：Nginx 源 IP Allowlist 只允许 `monitoring-vps` 固定公网 IP，同时使用独立随机 Bearer/Basic Auth 凭据；未授权来源必须返回 403/404。
* [ ] 资源限制初始预算：Prometheus `768MB`、Grafana `384MB`、Alertmanager `128MB`、Blackbox Exporter `128MB`；监控栈总预算不得挤占系统和 Docker 的安全余量，部署后依据实测 RSS 调整。
* [x] FrontierCloud 容器内健检每 30 秒执行且不写请求日志；Redis 使用 120 个分钟槽循环记录最近 120 分钟成功/失败计数，旧槽覆盖、停止探测后键自动过期。RN 继续负责独立公网探测和连续异常告警。
* [ ] CPU > 90%、Memory > 90% 等告警必须定义持续时间，避免瞬间尖峰触发告警。
* [ ] Disk Available < 8% 触发高优先级告警，并排除 tmpfs、overlay 等无需监控的文件系统。
* [ ] 带宽异常定义明确的阈值或近期基线规则，不使用模糊的“异常流量”判断。
* [ ] 增加告警抑制/冷却机制，避免重复发送相同告警。
* [ ] 增加异常恢复通知，并记录异常开始时间、恢复时间和持续时间。
* [ ] Alertmanager 仅配置 Telegram Bot 通知，不配置 Email Receiver；Bot Token 和 Chat ID 从 `monitoring-vps` 的 `.env`/Docker Secret 注入。
* [ ] 部署程序使用 Bot Token 调用 `getMe` 验证机器人身份；用户先向机器人发送 `/start`，再通过 `getUpdates` 获取 Chat ID。整个过程不得打印、回显或记录 Bot Token。
* [ ] Telegram 告警必须覆盖 firing 与 resolved 两种状态，并设置分组、抑制、重复间隔和冷却，避免故障期间刷屏。
* [ ] 告警消息包含服务名、主机、指标值、阈值、开始时间及可能的故障定位信息。
* [x] Grafana/Prometheus/Alertmanager 由 RN HTTPS 网关按 `/grafana/`、`/prometheus/`、`/alertmanager/` 子路径发布；禁止直接映射三个后端端口。

## 匿名聊天墙重构（当前实现）

匿名墙安全目标：

服务端永不长期保存聊天明文。
聊天消息仅支持三种内容形态：文本、表情、图片。
文本与表情可以混合存在于同一条消息中；图片必须作为独立的一条消息存在，不能和文本/表情混排。
文本、表情及图片内容优先由发送端浏览器本地加密。
服务端持久层只保存密文和必要的最小元数据。
解密密钥与业务数据完全分离。
消息密钥使用独立、无持久化的 Key Store 保存。
Message Key 不进入 MySQL Backup、Redis RDB/AOF 或其他任何长期备份。
Reveal 必须是严格的一次性原子操作。
成功签发一次解密能力后，服务器立即将该消息标记为 Burned 并销毁服务器侧解密能力，不等待客户端确认。
同一条消息第二次 Reveal 永远返回 410 Gone 或等价不可恢复状态。
移动端只有在用户持续双指按住消息区域期间，才允许执行本地解密并显示明文。
桌面端不提供 Reveal 能力，不能看到消息明文或图片内容。
touchend、touchcancel、pagehide、页面失焦/离开等事件发生后，客户端立即删除明文 DOM/Canvas 内容，清理应用层可控 Buffer、CryptoKey 引用及相关 Worker。
文本消息、文本+表情消息和图片消息全部遵循同一套一次性 Reveal / Burn 生命周期。
图片本身作为加密 Blob 存储；Message Key 被销毁后，即使加密图片文件尚未完成物理 GC，也不得具备从服务端恢复原图的能力。
Message Key 销毁后，对对应 ciphertext/blob 执行异步 GC。
Nginx、FastAPI、Redis、MySQL、Grafana、Prometheus 均禁止记录聊天正文、表情内容、图片明文、Message Key、Reveal Key 及任何足以恢复内容的秘密材料。
匿名墙采用独立的数据最小化日志策略，不记录请求 Body，不在 URL/Query 中携带消息正文、密钥等敏感信息。
监控系统只保留服务安全、性能和故障排查所需的聚合指标，不允许 message_id、session_id、正文 Hash 等高敏感/高基数字段进入 Prometheus Label。
滥用检测和限流数据设置短 TTL，只用于安全控制，不建立长期设备或用户画像。
服务端被事后完整攻陷时，对于已经 Burn 的消息，应无法仅依赖服务器当前数据、日志、数据库、缓存或备份恢复其历史正文、表情语义或图片内容。
不承诺阻止接收者在 Reveal 期间主动截图、录屏、修改客户端、控制操作系统或使用外部设备记录已经获得的明文。
未 Reveal 的消息仍需设置最大生命周期 TTL；到期后即使无人查看，也自动销毁 Message Key，并异步清理密文，避免匿名墙成为长期密文仓库。

### 临时匿名身份

* [x] 每次进入聊天墙前从头像池中选择一个头像。
* [x] 使用随机 HttpOnly Cookie + Redis TTL 与当前浏览器会话绑定，有效期 15 分钟。
* [ ] Session 建议使用随机标识 + Redis TTL，不使用 Canvas/字体/显卡等浏览器 Fingerprint 技术建立长期设备画像。
* [ ] 头像池采用无明显用户偏好画像特征的统一风格二次元/卡通头像。
* [ ] 头像资源独立管理，可动态启用、禁用和扩充头像池。

### 临时揭示消息

* [x] 默认只显示密文类型、匿名头像与剩余时间，不直接展示正文。
* [x] 用户必须先单击选择目标消息，然后在移动端双指持续按住揭示区；不足双指、松手、失焦、切后台或离页都会立即清除明文 DOM、可控 Buffer 与 Blob URL。
* [x] 不为细指针桌面端设计等价揭示操作；只允许至少双点且主指针为触摸的设备执行 Reveal。
* [ ] 明确定义消息是否只能揭示一次、刷新后是否还能查看、揭示后何时从服务器删除。
* [ ] 明确定义附件是否和正文使用同样的销毁规则。
* [ ] UI/文档明确该功能属于临时揭示及旁观防护，无法阻止截图、录屏、开发者工具或主动抓包获取已经发送到客户端的内容。

### 文件、图片和表情

* [x] 消息支持文本、文本内置表情或单张图片；通用附件不进入本轮实现。
* [x] 不支持视频。
* [x] 图片单文件最大 20MB，并在浏览器安全解码、限制像素后重新编码为 WebP，以清除 EXIF/GPS 等元数据。
* [ ] 明确定义普通附件最大尺寸、单条消息附件数量和总体存储限制。
* [ ] 表情统一使用服务端认可的表情库/ID，不允许客户端任意提交 HTML。
* [ ] 服务端校验文件真实类型和文件大小，不仅依赖文件扩展名及客户端 MIME。
* [ ] 上传文件使用随机生成的存储名称，禁止直接使用用户输入路径。
* [ ] 上传目录禁止作为可执行 Web 目录。
* [ ] 图片进行安全解码/重新编码，限制最大像素尺寸，防止异常图片造成内存消耗。
* [ ] 图片上传后清理 EXIF/GPS/设备等 metadata，避免匿名用户无意泄露位置和设备信息。
* [ ] 文件下载使用受控 endpoint，并设置正确的 `Content-Type` / `Content-Disposition`。
* [ ] 消息过期或销毁后建立附件垃圾回收机制，避免磁盘不断积累孤儿文件。
* [ ] 对匿名消息和附件上传增加基本速率限制以及资源配额，防止少量用户耗尽服务器磁盘或带宽。

## 每批发布要求

* [ ] 修改前确认当前生产版本 commit SHA，并保留可靠回滚点。
* [ ] 数据库 schema 修改使用可回滚 migration，并在生产 migration 前完成数据库备份。
* [ ] 本地/测试环境完成自动化测试。
* [ ] Docker 镜像构建成功。
* [ ] 全部计划内功能在本地原子提交完成并通过验证后，统一推送 Git 远程仓库；目标为一次推送。
* [ ] 生产环境拉取确定的 commit/tag。
* [ ] 部署生产 Docker 服务。
* [ ] 验证 Nginx → FastAPI → Redis/MySQL 完整业务链路。
* [ ] 验证新增功能以及原有核心功能无回归。
* [ ] 检查 Docker logs、Nginx logs 以及服务健康状态。
* [ ] 发布失败时回滚到上一生产 commit/image/database 状态。
* [ ] 修复后重新执行测试、提交、部署和业务验证流程。
* [ ] 每个功能使用独立原子提交；Release Batch 与 commit 数量不强绑定，但任何 commit 都不得包含超过一个功能性变化。

## 生产运维入口

管理员提权凭据由 Web 容器每 15 分钟签发。始终使用稳定容器名读取最新一条，不使用会随重建改变的容器 ID：

```bash
docker logs office_automation_web 2>&1 \
  | grep -F '[ADMIN_TOKEN] temporary admin token=' \
  | tail -n 1
```

复制 `temporary admin token=` 后面的值，在 `/api/v1/media` 点击“提权”。凭据过期时等待下一条签发日志，不需要重启服务。

RN 监控入口由 `MONITORING_SERVER_NAME` 决定：

```text
https://<MONITORING_SERVER_NAME>/grafana/
https://<MONITORING_SERVER_NAME>/prometheus/
https://<MONITORING_SERVER_NAME>/alertmanager/
```

Grafana 使用 `.env` 中的 `GRAFANA_ADMIN_USER` 与 `GRAFANA_ADMIN_PASSWORD`。Prometheus、Alertmanager 共用 `MONITORING_BASIC_USER` 与 `MONITORING_BASIC_PASSWORD`，但由两个服务各自的 `--web.config.file` 验证 bcrypt 哈希。只有网关发布 `80/443`；不要重新映射 `3000/9090/9093`。

首次或凭据轮换后，在 RN 生成 bcrypt 值、渲染受限运行时文件并重建监控栈：

```bash
cd /opt/frontiercloud-monitoring/app/monitoring
docker run --rm -it --entrypoint htpasswd httpd:2.4-alpine \
  -nBC 12 "$MONITORING_BASIC_USER"
python3 render_config.py
docker compose config --quiet
docker compose up -d --remove-orphans
```

不要把命令输出的用户名部分写入 `MONITORING_BASIC_PASSWORD_HASH`；该变量只保存冒号之后的 bcrypt 哈希，并用单引号包裹以保留 `$`。证书续期后必须再次执行 `render_config.py` 并重建 `gateway`，以把新证书复制到仅网关用户可读的 Secret 文件。






CentOS 本地 HTTP-only 开发部署只修改 `.env`，不要改 Compose 文件：
```dotenv
ENVIRONMENT=development
COMPOSE_PROFILES=
SERVER_NAME=_
HTTP_PORT=8080
HTTPS_PORT=8443
SSL_CERT_PATH=/dev/null
SSL_KEY_PATH=/dev/null
ADMIN_COOKIE_SECURE=false
ADMIN_COOKIE_SAMESITE=lax
ADMIN_COOKIE_NAME=admin_session
ADMIN_CSRF_COOKIE_NAME=admin_csrf
WEBRTC_STUN_HOST=localhost
WEBRTC_STUN_URLS=
```

`development` 下 Nginx 只监听容器内 HTTP 端口，不加载证书；`HTTPS_PORT`
只是保留的 Compose 端口映射，连接会被拒绝。CentOS 默认的 rootless Podman
不能绑定 1024 以下端口，因此本地示例使用 8080。启动命令为
`podman compose up -d --build`；使用 Docker 时命令仍是
`docker compose up -d --build`。



首次拉取：
```bash
git clone https://github.com/wongyiuming/FrontierCloud.git
cd FrontierCloud
cp .env.example .env
docker compose down && docker compose up -d --build
```

更新:
```bash
git pull https://github.com/wongyiuming/FrontierCloud.git
docker compose build
docker compose up -d
```

## 本地 Python 环境

项目继续兼容标准 `pip`，也提供 `uv.lock` 供 uv 复现依赖。



## ⚠️ Windows 部署注意事项（LTSC / Server 版本）

如果你在 **Windows LTSC** 或 **精简系统（如 Server Core）** 上运行该项目，请务必先安装：

👉 [Microsoft Visual C++ Redistributable (x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe)

否则，`pymupdf`（即 `fitz`）模块会报错：
