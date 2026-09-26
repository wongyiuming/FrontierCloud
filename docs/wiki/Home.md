# FrontierCloud Wiki

FrontierCloud 是一个自托管的媒体浏览、播放、卡拉 OK、资源节点与运维管理系统。项目以 FastAPI 为业务与控制平面，浏览器端使用原生 JavaScript，Docker Compose 负责 Web/API、Nginx、MySQL、Redis、Updater 与 STUN 等运行组件。

本 Wiki 面向两类读者：

- **运维人员**：部署、节点接入、存储分布、Compute、Backup、升级、回滚、排障。
- **开发人员**：数据模型、集群边界、数据库迁移、发布规则、CI 与代码约束。

## 文档导航

- [总体架构](Architecture.md)
- [部署与配置](Deployment-and-Configuration.md)
- [节点、集群与资源池](Cluster-and-Resource-Model.md)
- [媒体目录与存储 Placement](Media-and-Storage.md)
- [运维与排障](Operations-and-Troubleshooting.md)
- [发布、升级、回滚与数据库迁移](Release-and-Database-Migrations.md)
- [开发与 CI](Development-and-CI.md)
- [常用 Bash / SQL](Command-Reference.md)

## 核心原则

### Master 是唯一业务主节点

FrontierCloud 集群只有一个业务 Master。Master 持有全局媒体目录、业务数据库、歌词关系、播放统计、用户、审计等业务事实。

Follower 不拥有独立业务目录。Follower 是资源节点，可独立提供 Storage、Compute、Backup 中任意一种或多种能力。

### 全局媒体目录是权威来源

媒体文件的“文件路径”和“实际存在哪个节点”不是靠扫描目录临时推断，而是由 Master 的全局目录记录。

核心关系：

```text
global_media_objects.storage_member_id
                │
                ▼
cluster_storage_members.member_id
                │
                └── relationship_id ──► node_relationships
```

因此一个媒体对象可以明确回答：

- media_id 是什么；
- path 是什么；
- 实际位于哪个 Storage Member；
- 对应哪个 Follower；
- 当前节点健康状态；
- 访问走 Local / Direct / Relay 中的哪一种路径。

### 配置状态与实际状态必须分开

节点配置采用 Desired / Observed 思路：Master 保存的配置只是“期望”，Follower 心跳确认后才是“已生效”。Storage、Compute、Backup 的 UI 与 API 都应区分配置是否已经真正下发并应用。

### 发布系统必须 Fail Closed

新版本进入生产必须满足发布验证。GitHub 或 CI 验证数据暂时不可用时，当前业务继续运行，但新的升级不会被授权。

### 数据库禁止靠重建空库升级

FrontierCloud 使用版本化 Schema Generation。已有正式数据库通过逐代 migration 升级，不再因字段或表结构变化要求清空重建。

## 主要运行组件

| 组件 | 作用 |
| --- | --- |
| Nginx | HTTPS/HTTP 入口、静态资源、反向代理、维护模式与边缘安全 |
| Web | FastAPI 业务 API、Admin、集群控制、目录与媒体逻辑 |
| MySQL | 业务事实、全局目录、节点关系、任务、审计、迁移历史 |
| Redis | 运行期缓存与部分协调数据 |
| Updater | 本机版本构建、替换、升级、回滚与集群发布协调入口 |
| STUN / Coturn | WebRTC 网络观测所需 STUN 服务 |

## 重要术语

**Standalone**  
尚未固定为 Master/Follower 的独立节点。

**Master**  
唯一业务节点，拥有业务目录与集群控制权。

**Follower**  
资源节点。对公网业务页面不作为独立业务站点运行。

**Storage Member**  
可承载媒体对象的存储成员，包括 Master Local 和已启用 Storage 的 Follower。

**Compute Slot**  
Follower 可并行执行的 Worker 任务槽位上限。

**Backup Member**  
用于接收 Master 业务恢复包的 Follower。Backup 是恢复能力，不是在线数据库副本，也不是自动主备切换。

**Generation**  
数据库 Schema 的正式版本代际。

**Placement**  
某个媒体对象实际放置在哪个 Storage Member 上的关系。

## 先读哪几页

如果你负责部署：先看 [部署与配置](Deployment-and-Configuration.md)。

如果你在查“一个文件到底在哪台机器”：看 [媒体目录与存储 Placement](Media-and-Storage.md)。

如果你在查节点状态、Compute、Backup：看 [节点、集群与资源池](Cluster-and-Resource-Model.md)。

如果你准备升级生产：看 [发布、升级、回滚与数据库迁移](Release-and-Database-Migrations.md)。

如果你遇到故障：直接看 [运维与排障](Operations-and-Troubleshooting.md) 与 [常用 Bash / SQL](Command-Reference.md)。
