# 发布、升级、回滚与数据库迁移

## 1. 分支与发布规则

项目当前正式流程：

```text
dev
 ↓
完整 CI
 ↓
dev → main PR
 ↓
人工审核 / 合并
 ↓
main
 ↓
生产发布验证
```

未经明确确认，不额外创建 feature/fix 分支。

`main` 是生产发布权威分支，`dev` 是持续开发与验收分支。

## 2. 为什么不能只看 main HEAD

一个 SHA 出现在 `main` 并不足以证明它经过了正式开发验收。

FrontierCloud 发布验证要求：

1. 当前 `main` HEAD 能关联到且只能关联到一个已合并、同仓库的 `dev → main` PR；
2. 该 PR 的 head SHA 有 exact `dev` push CI；
3. 对应 CI 完成且成功；
4. PR head tree 与当前 main HEAD tree 完全一致；
5. 任一信息缺失、歧义或不一致都 fail closed。

这样发布安全不依赖 GitHub 使用 merge/squash/rebase 中哪一种合并方式。

## 3. GitHub 发布验证

发布系统会访问 GitHub REST API 读取：

- main branch HEAD；
- 与 main commit 关联的 PR；
- dev push workflow run；
- dev source commit/tree。

### 匿名 API 限流

GitHub 匿名 REST 请求存在较低的请求额度。

FrontierCloud 已实现：

- 发布校验缓存；
- `GITHUB_API_TOKEN` 可选认证；
- 403/429 rate-limit 识别；
- `x-ratelimit-*` / `retry-after` 解析；
- 服务端 backoff；
- backoff 期间手工刷新也不会继续撞 API；
- last-known-good 验证信息只用于展示。

### Fail Closed

GitHub 当前不可验证时：

```text
当前业务继续运行
当前集群状态继续展示
允许依据本地状态判断 rollback
禁止授权新的 upgrade
```

last-known-good 不能代替当前发布授权。

## 4. Updater

Updater 控制当前节点的发布状态，典型字段：

```text
release_branch
current_sha
previous_sha
target_sha
state
phase
detail
```

发布主要阶段可抽象为：

```text
校验
 ↓
构建
 ↓
替换
 ↓
分发
 ↓
完成
```

Master 负责本地替换以及集群分发协调。

## 5. Maintenance

升级过程中进入 maintenance 是为了防止在版本不一致或替换过程中继续接受不安全业务流量。

发布失败后，系统可能保持 maintenance，而不是自动假装恢复成功。

遇到这种情况首先查：

- updater state；
- target/current/previous SHA；
- 哪个 Follower 失败；
- 失败发生在 build / replace / distribute 哪一步。

不要先强制关 maintenance 再查原因。

## 6. Rollback

Rollback 目标来自 Updater 保存的 previous web-managed SHA。

rollback 和 upgrade 一样需要检查集群发布边界，但它不是依赖 GitHub 当前最新 main 来决定本地 previous SHA。

在回滚前仍应确认数据库兼容性：代码回滚不意味着 Schema 自动 downgrade。

## 7. 数据库 Schema Generation

正式数据库由：

```text
frontiercloud_schema
```

保存当前 generation。

当前版本化迁移框架位于：

```text
app/core/schema_migrations.py
```

迁移历史表：

```text
frontiercloud_schema_migrations
```

记录：

```text
generation
migration_name
checksum
applied_at
```

## 8. 新数据库与旧数据库

### 新空库

直接创建当前最新 Schema，并写入当前 Generation。

不会为了历史兼容从 Generation 1 一路执行所有旧 migration。

### 已初始化旧库

读取 generation，然后逐代升级：

```text
Generation 1
   ↓ migration
Generation 2
   ↓ migration
Generation 3
   ↓ ...
Current Generation
```

迁移链必须连续。

## 9. 为什么不用“一个大 SQL 自动 ALTER”

未来 Schema 改动是未知的，不可能提前写一个万能迁移器。

正确方法是每一次实际 Schema 变化都增加一代明确 migration。

例如未来需要给 `cluster_worker_jobs` 增加 `priority`：

```python
async def _migration_2_to_3(conn):
    await add_column_if_missing(
        conn,
        "cluster_worker_jobs",
        "priority",
        "INT NOT NULL DEFAULT 0",
    )
```

然后注册为 Generation 3。

同时最新空库的 bootstrap schema 直接包含新字段。

## 10. MySQL DDL 的安全模型

MySQL DDL 会出现 implicit commit，所以不能声称：

```text
ALTER 1 成功
ALTER 2 失败
ROLLBACK
=> 所有 Schema 变化全部消失
```

这并不可靠。

FrontierCloud 采用：

```text
MySQL advisory lock
+
逐代 migration
+
幂等 DDL
+
成功后才推进 generation marker
+
失败保持旧 generation
+
下次启动安全重试
```

## 11. Migration Lock

迁移使用数据库级 advisory lock，避免多个 Web 实例同时修改 Schema。

流程：

```text
Web A / B / C 同时启动
         ↓
竞争 schema migration lock
         ↓
A 获得
         ↓
A 完成 migration
         ↓
A 推进 generation
         ↓
B 获得 lock 后重新读取 generation
         ↓
发现已是最新 → no-op
```

注意：获得 lock 后必须重新读取 generation，不能相信等待之前的版本值。

## 12. 幂等 Migration

迁移辅助函数包括：

```text
table_exists
column_exists
index_exists
add_column_if_missing
add_index_if_missing
```

假设迁移执行一半进程崩溃：

```text
column A 已成功
column B 尚未执行
```

下一次启动：

```text
A 已存在 → 跳过
B 不存在 → 执行
```

而 generation marker 仍停留在旧版本，直到整代成功。

## 13. Migration 失败

如果某一代失败：

- rollback 当前未提交事务；
- 不推进 generation；
- 应用拒绝继续启动；
- 运维修复根因；
- 下次启动重新执行同一代幂等 migration。

这比把数据库错误标记为“已升级”安全得多。

## 14. 未来版本数据库

如果：

```text
Database Generation = 6
App Generation = 4
```

应用拒绝启动。

旧应用不会尝试：

```text
6 → 4
```

Schema downgrade 不自动执行。

因此生产 rollback 必须考虑“代码能否继续读取升级后的 Schema”。新增 migration 应尽量保持向后兼容，破坏性 migration 需要单独设计 release strategy。

## 15. 无 Marker 的历史数据库

非空数据库如果没有 `frontiercloud_schema` marker，程序不会根据“看起来像这些表”自动猜版本。

原因：它可能来自：

- 未知历史 commit；
- 手工建库；
- 残缺恢复；
- 开发测试数据；
- 非 FrontierCloud 数据。

自动 ALTER 这种数据库风险高于拒绝启动。

## 16. 如何增加下一代 Migration

标准步骤：

```text
1. 更新最新 bootstrap Schema
2. 新增 migration_N_to_N+1
3. migration 必须可重复执行
4. 注册 SchemaMigration
5. 更新/新增测试
6. 做真实 MySQL upgrade smoke
7. dev CI 全绿
8. PR → main
9. 再执行生产升级
```

Migration 的目标不是“让测试通过”，而是让已经运行数月/数年的正式数据库能无损继续升级。
