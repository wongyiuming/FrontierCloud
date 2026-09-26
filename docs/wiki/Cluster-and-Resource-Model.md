# 节点、集群与资源池

## 1. 节点角色

FrontierCloud 节点有三种角色：

- `Standalone`
- `Master`
- `Follower`

### Standalone

默认状态。节点尚未固定加入正式集群。

### Master

唯一业务主节点，拥有：

- 业务 API；
- 全局媒体目录；
- 节点配置权；
- Storage/Compute/Backup 调度事实；
- 发布协调；
- 业务数据库与审计。

### Follower

资源节点。可开启：

- Storage；
- Compute；
- Backup。

三个能力互相独立，不要求同时开启。

## 2. 节点关系

节点关系保存在 `node_relationships`。

主要字段包括：

```text
relationship_id
peer_id
peer_endpoint
direction
mode
state
status
last_heartbeat
rtt_ms
failures
recoveries
peer_version
protocol
summary
```

Master 视角下，Follower 关系通常是 downstream；Follower 对 Master 为 upstream。

## 3. 心跳

节点通过周期心跳同步：

- 在线状态；
- RTT；
- 连续失败；
- 恢复次数；
- Storage 实际状态；
- Compute 实际状态；
- Backup 实际状态；
- 本机资源指标；
- 部分运行摘要。

Admin UI 中的：

```text
Current RTT
Avg RTT
Min RTT
Max RTT
Last heartbeat
Failure count
Recovery count
```

是为了判断真实链路状态，不应把单次 RTT 误认为长期平均值。

## 4. Desired / Observed

Master 上的资源开关是 Desired State。

例如：

```text
Compute = enabled
worker_slots = 4
```

只代表 Master 已保存配置。

Follower 在下一轮控制心跳收到并实际应用后，才会把 Observed State 回报给 Master。

UI 状态应该理解为：

```text
配置已保存
    ↓
等待 Follower 确认
    ↓
Desired == Observed
    ↓
已生效
```

如果节点离线：

```text
配置已保存，但尚未生效
```

不能把 HTTP POST 成功当成 Follower 已应用成功。

## 5. Storage

Storage Member 表：

```text
cluster_storage_members
```

关键字段：

```text
member_id
relationship_id
member_kind
transport
storage_enabled
allocated_bytes
used_bytes
reserved_bytes
physical_free_bytes
health
writable
updated_at
```

### allocated_bytes

管理员允许 FrontierCloud 使用的逻辑容量。

### used_bytes

程序报告的当前已使用容量。

### reserved_bytes

已被上传/任务等流程预留，但可能尚未完全转化为 active 媒体的容量。

### physical_free_bytes

底层文件系统真实可用空间。

### writable

是否允许继续放置新对象。

上传 placement 不只看一个容量数字，还需要同时满足节点健康、可写以及逻辑/物理空间条件。

## 6. Compute

Compute Member 表：

```text
cluster_compute_members
```

关键字段：

```text
member_id
enabled
worker_slots
available_slots
cpu_percent
memory_available_bytes
capabilities
updated_at
```

### worker_slots

该 Follower 允许同时执行的最大 Worker 任务数。

例如：

```text
worker_slots = 4
running = 2
```

表示理论上还有 2 个并发槽位可以执行任务。

### Worker Jobs

任务表：

```text
cluster_worker_jobs
```

记录：

```text
job_id
job_type
media_id
member_id
payload
result
state
lease_token_hash
lease_expires_at
attempts
created_at
updated_at
```

当前任务类型可包括媒体 hash、probe、metadata 等工作。

### 为什么任务会分给某个 Follower

主要两种原因：

**Pinned**

任务明确指定 `member_id`，只由指定 Follower 领取。

**Capability + FIFO**

任务没有指定节点；满足 capability 的 Follower 竞争领取，遵循任务创建顺序，成功 lease 的节点执行。

因此当前系统不是复杂的 CPU/内存打分调度器。CPU 和内存已经用于可观察性，但不能把它描述成 Kubernetes/Nomad 式综合 placement scorer。

## 7. Worker Lease

Worker 任务不是简单“取出来就算完成”。

任务通过 lease 控制：

```text
queued
  ↓
leased/running
  ↓
completed
```

异常情况下 lease 可过期并重试。`attempts` 用于观察重试次数。

这使任务具备一定的失败恢复能力，避免节点领取后崩溃导致任务永久消失。

## 8. Backup

Backup Member 表：

```text
cluster_backup_members
```

关键字段：

```text
member_id
enabled
generation
last_success
lag_seconds
checksum
state
updated_at
```

Follower 保存的具体恢复包元数据位于：

```text
cluster_business_backups
cluster_business_backup_chunks
```

Backup 状态重点看：

- 是否 Enabled；
- Desired 是否已生效；
- 最近一次成功时间；
- 最近一次尝试结果；
- 最近恢复点大小；
- checksum；
- 恢复点数量；
- 下次计划时间。

### generation

Backup generation 是恢复点唯一代际 ID，主要服务机器判断，不是管理员日常最需要看的字段。UI 主视图应优先显示最近成功时间和状态。

### Backup 不是什么

Backup 不是：

- MySQL Group Replication；
- 主从数据库在线复制；
- 自动 Master 选举；
- 自动 failover。

它是可校验的业务恢复包。

## 9. 节点状态判断

建议按以下顺序判断节点：

```text
1. Relationship 是否 active
2. 最近心跳是否新鲜
3. RTT 与失败次数是否异常
4. Desired 是否等于 Observed
5. Storage/Compute/Backup 各自运行结果是否正常
6. 节点版本是否与集群目标一致
```

不能只看一个“绿色开关”。

## 10. 节点撤销和重新初始化

这是危险操作。

如果节点仍然承载：

- active media；
- pending delete；
- 用户录音；
- 其他受保护资源；

系统应拒绝直接撤销或重置，避免目录仍引用某个已经不存在的节点。

在做关系撤销之前应先确认媒体 placement 已迁移或删除完成。

## 11. 版本状态

节点发布状态与资源状态独立。

一个 Follower 可能：

```text
Storage healthy
Compute healthy
Backup healthy
```

但版本还落后于 Master。

反过来也可能所有节点版本一致，但 Backup 最近一次执行失败。

Admin UI 将这两个维度分开显示是有意设计，不应合并为一个模糊的“节点正常”。
