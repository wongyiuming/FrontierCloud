# 总体架构

## 1. 架构概览

FrontierCloud 采用“一个业务 Master + 多个资源 Follower”的集群模型。

```text
                    ┌──────────────────────┐
                    │      Browser         │
                    └──────────┬───────────┘
                               │ HTTPS/HTTP
                               ▼
                    ┌──────────────────────┐
                    │        Nginx         │
                    └──────────┬───────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │   FastAPI / Web      │
                    │      Master          │
                    └──────┬───────┬───────┘
                           │       │
                   MySQL / Redis   │ signed cluster control
                           │       │
                           ▼       ▼
                    ┌─────────┐  ┌─────────────────┐
                    │Business │  │ Follower nodes  │
                    │ facts   │  │ Storage/Compute │
                    │Catalog  │  │ Backup          │
                    └─────────┘  └─────────────────┘
```

前端不依赖 React/Vue 等 SPA 框架，主要使用原生 HTML/CSS/JavaScript。媒体播放器由原生浏览器媒体 API 驱动，卡拉 OK 使用现有 Rust/WASM 模块与浏览器音频能力协同。

## 2. 控制平面与数据平面

### 控制平面

负责：

- 节点身份与角色；
- Master/Follower 配对；
- 心跳与状态同步；
- Storage/Compute/Backup 配置；
- 发布升级与回滚；
- Admin 操作与审计；
- 数据库 Schema Generation。

关键数据表包括：

- `node_identity`
- `node_relationships`
- `node_audit`
- `cluster_storage_members`
- `cluster_compute_members`
- `cluster_backup_members`
- `cluster_worker_jobs`

### 数据平面

负责：

- 媒体上传；
- 媒体读取与播放；
- Direct / Relay 传输；
- Follower 本地媒体对象访问；
- Compute Worker 任务；
- Master 业务恢复包向 Backup Follower 传输。

控制平面状态和数据平面结果不能混为一谈。例如“Backup 开关已打开”只是 Desired 配置，“最近一次备份成功”才是实际运行结果。

## 3. 业务数据归属

### Master 拥有的业务事实

Master 是唯一权威业务节点，主要拥有：

- 全局媒体目录；
- 媒体路径与稳定 media_id；
- 歌词文件与歌词关联；
- 播放统计与偏好；
- 用户与 Admin 业务状态；
- 节点关系和审计；
- Compute 任务事实；
- Backup 元数据；
- Schema Generation 与 migration journal。

### Follower 拥有的本地资源

Follower 可以保存：

- 被 Master 分配到本节点的媒体对象；
- 本节点执行 Compute 所需或产生的临时运行状态；
- Master 业务备份恢复包；
- 心跳上报所需本机资源指标。

Follower 不应该自行形成第二套业务目录。

## 4. 媒体对象身份

一个媒体对象至少涉及以下几个概念：

- `media_id`：Master 全局目录中的稳定媒体标识；
- `media_path`：逻辑业务路径；
- `storage_member_id`：实际存储位置；
- `object_id`：存储节点本地对象标识；
- `path_locator`：用于路径唯一性校验的定位值；
- `state`：如 active / pending_delete 等对象状态。

`global_media_objects` 是媒体 placement 的权威表。

## 5. Storage Pool

存储池由：

- Master Local；
- 所有启用 Storage 的 Follower；

共同组成。

`cluster_storage_members` 保存每个存储成员的：

- 分配容量；
- 已使用容量；
- 保留容量；
- 物理可用空间；
- health；
- writable；
- transport；
- relationship_id。

上传时需要选择满足条件的 writable Storage Member。一个逻辑路径只对应一个完整媒体对象 placement，不做文件分片式跨节点存储。

## 6. Compute Pool

Compute Follower 通过 `cluster_compute_members` 声明：

- enabled；
- worker_slots；
- available_slots；
- CPU 使用率；
- 可用内存；
- capabilities。

Worker 任务记录在 `cluster_worker_jobs`。

当前任务分配语义主要有两类：

1. **Pinned**：任务明确指定某个 Follower；
2. **Capability + FIFO**：任务未指定节点，只要能力匹配，最先成功 lease 到任务的可用节点执行。

节点 UI 中的 slot 代表真实并发任务上限，而不是单纯配置展示值。

## 7. Backup

Backup Follower 接收的是 Master 生成的业务恢复包，而不是 MySQL 在线物理副本。

主要特性：

- 分块传输；
- SHA-256 完整性校验；
- generation 标识恢复点；
- 最近成功时间；
- 恢复点数量；
- 最近尝试状态；
- 失败后较短周期重试。

Backup 的目标是灾难恢复，不是自动 failover。

## 8. 网络与传输

节点之间固定角色要求证书验证的 HTTPS。

媒体访问根据资源位置与网络能力决定 Local / Direct / Relay：

- **Local**：资源在当前 Master 本地；
- **Direct**：浏览器或请求方直接访问资源 Follower；
- **Relay**：由 Master/受控路径中继数据。

公网业务页面以 Master 为入口。Follower 的业务 API 不是独立业务站点。

## 9. 数据库

MySQL 是业务与集群事实的主要持久化数据库。

Redis 用于运行时缓存或协调，但不能作为需要恢复的唯一业务事实来源。

正式数据库使用 `frontiercloud_schema` 保存 Schema Generation，并使用 `frontiercloud_schema_migrations` 记录迁移历史。

## 10. Updater 与发布架构

Updater 是独立控制组件，负责：

- checkout/验证目标 SHA；
- 构建 Web/Nginx 等镜像；
- 替换本地容器；
- 驱动 Follower 升级；
- 集群发布状态；
- rollback；
- maintenance 状态。

生产发布只认 `main`，但 `main` 的目标必须能证明来自被审核的 `dev → main` PR，并且 PR 的 dev head 有 exact dev push CI 成功且代码树与 main 一致。

## 11. Fail-Closed 边界

以下情况应拒绝继续危险操作，而不是“猜测没事”：

- 固定节点角色但 TLS/证书条件不满足；
- GitHub/CI 无法确认新的发布目标；
- 数据库来自比当前程序更高的 Generation；
- 数据库迁移失败；
- Follower 配置尚未确认生效；
- 节点包含受保护媒体/录音但请求撤销或重置；
- 集群发布无法确认所有节点达到目标。

FrontierCloud 的目标是：业务可以在外部依赖短暂失败时继续运行，但新增危险变更必须保守拒绝。
