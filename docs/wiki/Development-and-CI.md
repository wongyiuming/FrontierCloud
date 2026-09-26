# 开发与 CI

## 1. 分支规则

默认开发流程：

```text
dev
 ↓
push CI
 ↓
dev → main PR
 ↓
人工合并
 ↓
main
```

不要默认创建额外 feature/fix 分支。确实需要新分支时，先得到仓库维护者明确同意。

不要直接向 `main` 推送普通开发提交。

## 2. 技术栈

主要技术：

- Python / FastAPI；
- SQLAlchemy async；
- MySQL；
- Redis；
- Nginx；
- Docker Compose；
- 原生 HTML/CSS/JavaScript；
- Rust/WASM 卡拉 OK 模块；
- GitHub Actions。

## 3. 代码目录概览

```text
app/
  api/                 API 与 Admin endpoints
  core/                配置、数据库、Schema migration 等核心模块
  services/            业务服务、集群、发布、节点观测等

static/
  js/                  Admin、媒体浏览器、播放器等浏览器逻辑

nginx/                  Nginx 镜像与配置
updater/                Updater 控制组件
scripts/                CI/source policy 等脚本
tests/                  单元、运行期、浏览器、集群与回归测试

docker-compose.yaml     默认部署拓扑
Dockerfile              Web 镜像
README.md               快速入口
docs/wiki/              详细 Wiki
```

## 4. 本地基础检查

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

```bash
node tests/admin_ui_smoke.mjs
node tests/player_cache_smoke.mjs
```

```bash
docker compose config --quiet
```

实际提交前还需要以 GitHub Actions 的最终结果为准。

## 5. CI 总体结构

核心 workflow：

```text
.github/workflows/docker.yml
```

主要 Gate：

```text
verify-promotion-query
browser-ui
test-cluster
        │
        └──────┐
               ▼
          test-compose
```

`test-compose` 是聚合 Gate，在执行自身测试前检查上游 Gate 结果。

### verify-promotion-query

验证 dev push 的 exact SHA 查询逻辑，确保发布控制查询不会匹配错误的 CI run。

### browser-ui

启动真实应用栈并使用 Chromium 做浏览器回归。

目的不是检查 HTML 字符串，而是验证真实页面可以被浏览器加载和交互。

### test-cluster

构建真实多节点/双节点 HTTPS 拓扑，验证：

- 固定角色；
- 配对；
- 心跳；
- 资源配置；
- Direct / Relay；
- 集群控制；
- Follower 行为。

### test-compose

聚合并运行：

- source configuration tests；
- frontend syntax；
- Compose build/start；
- unit/runtime；
- security / edge；
- public/admin flows；
- 最终 cleanup。

## 6. Exact-SHA 原则

任何最终结论必须对应当前最终 head SHA。

错误做法：

```text
SHA A CI 绿
↓
又提交 SHA B
↓
因为改得少，所以拿 A 的绿灯证明 B
```

正确做法：

```text
最终 SHA B
↓
B 自己跑完整 CI
↓
根据 B 的结果决定是否可合并
```

这是发布系统本身也遵循的规则。

## 7. Source Contract 与 Runtime Test

有些约束属于源码/部署契约，例如：

- 镜像版本必须 patch-pin；
- 某个 volume 必须只读；
- 发布 UI 必须位于正确模块；
- 某些安全配置不能退化。

这类检查应放在 source policy/configuration test，而不是为了让测试读取 Dockerfile 就把部署文件复制进业务镜像。

测试必须检查正确责任模块，不能因为历史代码移动了就强迫新模块保留无关文案。

## 8. 数据库改动规则

任何 Schema 改动必须：

1. 更新最新空库 Schema；
2. 增加明确 Generation migration；
3. migration 可重复执行；
4. generation 只在成功后推进；
5. 增加 migration test；
6. 尽可能增加真实 MySQL upgrade smoke。

禁止恢复旧做法：

```text
Schema 一变 → 要求生产清空数据库
```

## 9. 集群数据模型改动

修改 Storage/Compute/Backup 时先确认：

- Desired 与 Observed 是否仍可区分；
- Follower 离线时是否会错误显示“已生效”；
- 心跳同步是否会覆盖 durable state；
- UI 展示的是配置还是实际值；
- 是否破坏现有节点升级兼容。

例如 Backup 的 `last_success` 属于 durable result，不应被重复配置心跳覆盖为“从未成功”。

## 10. UI 开发规则

Admin UI 的目标不是简单展示数据库字段。

每个运维状态至少要回答：

```text
现在是什么状态？
期望是什么？
实际生效了吗？
最近一次成功/失败是什么时候？
为什么做出这个调度或决定？
下一步该看哪里？
```

例如 Compute 不应该只显示：

```text
Slots 4
```

而应展示：

```text
运行中 2 / 4
排队 7
24h 完成 126
失败/重试
CPU / 内存
任务为什么分到本节点
```

## 11. 发布代码改动规则

发布控制属于高风险区域。

修改以下内容必须补回归：

- GitHub provenance；
- exact dev CI；
- tree equality；
- rate limit；
- last-known-good；
- can_upgrade；
- rollback target；
- follower convergence；
- maintenance。

外部依赖不可用时可以继续展示旧可信事实，但不能用旧缓存授权新的危险操作。

## 12. 不要为了测试破坏生产设计

典型错误：

- 测试读取不到源文件，就把 Dockerfile/Compose 复制进业务镜像；
- 测试期待旧 UI 文案，就把无关文案重新塞回错误模块；
- 为了让 migration 测试绿，关闭真实 MySQL smoke；
- 为了通过发布测试，放宽 fail-closed 条件。

正确做法是修测试责任边界或真实实现。

## 13. Pull Request 内容

PR 建议明确写：

- 问题是什么；
- 为什么发生；
- 改了哪些文件/模块；
- 安全边界；
- 数据库/升级影响；
- 回归覆盖；
- 最终 exact SHA；
- CI run number。

不要只写“fix bug”。

## 14. 合并以后

PR 合并并不等于生产已经升级。

流程仍然是：

```text
PR merged to main
       ↓
release verifier 确认 provenance + dev CI + tree
       ↓
Admin 发起升级
       ↓
Master/Followers 构建替换
       ↓
集群收敛
       ↓
完成
```

开发完成、代码合并和生产发布是三个不同状态。
