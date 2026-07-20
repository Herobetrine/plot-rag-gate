# 力量体系引擎迁移与回滚

适用版本：`plot-rag-gate v1.3.0`

本文件说明旧项目如何从 continuity schema v4、旧 `ability` 投影和
`InitializationBundle v1` 迁移到 continuity schema v5、
`plot-rag-power/v1` 与可选的 `InitializationBundle v2`。

## 1. 兼容边界

- project config 继续使用 v3；新增的 `power_system` 配置块是可选项。
- continuity schema 从 v4 升级到 v5。
- `InitializationBundle v1` 继续可读；只有需要结构化力量模型的初始化才协商为 v2。
- 旧 `ability gain / set / use / cooldown / breakthrough / lose` 事件继续可重放。
- accepted commit、事件 ID、canon revision、来源引用和审计链不改写。
- 未建立完整力量定义的旧项目默认进入 `shadow` 或 `unmodeled`，不根据题材常识补造境界链、换算规则或克制关系。

## 2. 迁移前检查

```powershell
python -X utf8 .\scripts\plot_state.py doctor `
  --project-root "D:\novel"

python -X utf8 .\scripts\plot_state.py migrate `
  --project-root "D:\novel" `
  --component all `
  --dry-run
```

迁移前应确认：

1. `.plot-rag/state.sqlite3` 可读取；
2. 项目没有未处理的 schema 错误；
3. 当前 accepted proposal 和 canon revision 已记录；
4. Git 或项目备份已覆盖用户编写的正文、设定和大纲；
5. `SILICONFLOW_API_KEY` 只存在于进程环境，不在项目文件中。

## 3. 执行迁移

```powershell
python -X utf8 .\scripts\plot_state.py migrate `
  --project-root "D:\novel" `
  --component all

python -X utf8 .\scripts\plot_state.py replay `
  --project-root "D:\novel"

python -X utf8 .\scripts\plot_state.py doctor `
  --project-root "D:\novel"
```

schema 迁移会在 `.plot-rag/backups/` 创建 SQLite 在线备份，文件名包含旧
schema 版本、UTC 时间和原数据库哈希前缀。随后：

1. 从 active immutable accepted events 重放权威投影；
2. 把旧能力投影拆分为 ownership、runtime 和 use history；
3. 创建 progression、resource、status、binding、qualification、
   observation 与力量定义投影；
4. 重新计算投影哈希；
5. 保留旧兼容查询面。

若数据库只有孤立的旧 `ability_state` 行、没有相应 accepted 事件，迁移器会把它
转换成显式 legacy bootstrap 事件，并记录原数据库哈希和
`legacy_projection_import` provenance；不会把可变投影直接冒充原始正典。

## 4. 初始化协议升级

旧 `InitializationBundle v1` 不需要批量重写。需要完整力量模型时，对现有资料运行
一次只读 `ingest` 或 `hybrid`：

```powershell
python -X utf8 .\scripts\plot_state.py init dry-run `
  --workspace-root "D:\works" `
  --project-root "D:\novel" `
  --mode ingest `
  --target-profile continuity_ready `
  --source "D:\novel"
```

含有结构化力量 claim、显式 power profile 或力量对象的会话协商为
`plot-rag-init/v2`。v2 在 v1 世界、人物、剧情和来源字段之外增加：

- `power_systems`
- `progression_tracks`
- `rank_nodes`
- `rank_edges`
- `ability_definitions`
- `resource_definitions`
- `status_definitions`
- `qualification_definitions`
- `counter_rules`
- `bridge_rules`
- `conversion_rules`
- `actor_power_bootstrap`
- `power_model`

`ingest` 只整理已有证据；`hybrid` 只补阻塞目标档位的缺口。二者都先形成 proposal，
消费 `initialization_bundle` grant 后才 apply。

## 5. strict 与 shadow

建议按以下顺序启用：

1. 迁移 schema v5；
2. 保持 `power_system.mode=shadow`，运行查询和剧情准备；
3. 检查人物持有能力、境界、资源、冷却和来源绑定；
4. 通过初始化或独立 `power_spec_change` proposal 补齐权威定义；
5. 明确切换到 `strict`。

strict 模式下，非法跳阶、未获得能力、资源不足、冷却未结束、来源失效、
资格不足、无规则转换和端点类型错误会被确定性阻断或 quarantine。

## 6. 规则变更

世界级力量定义应与普通章节 delta 分离。请单独创建：

```text
proposal_kind = power_spec_change
grant operation = accept_power_spec
```

普通 `story_delta` 只更新角色运行态、使用记录和观察结果。一个 proposal 同时包含
力量定义与普通剧情事件时，整体进入 quarantine，拆分后分别审阅。

`compare_power_conditions` 返回的 `ComparisonClaim` 是查询时派生结果：
`derivation=query_time`、`persisted=false`。它只组合 accepted 定义、角色运行态、
知识平面、条件与逐事件证据，不写入正典，也不保存无条件胜负结论。

## 7. 回滚

回滚时先停止会写入该项目的 Hook、CLI 或 MCP 进程，再执行：

1. 复制当前 `.plot-rag/state.sqlite3` 作为故障取证副本；
2. 从 `.plot-rag/backups/` 选择迁移前生成的完整 SQLite 备份；
3. 用备份替换 `.plot-rag/state.sqlite3`；
4. 删除由该数据库产生、可再生的 snapshot/index/summary/memory/vector 投影；
5. 恢复迁移前 config；
6. 运行 `doctor`，确认 schema 与 config 匹配。

不要直接编辑 SQLite 行，也不要删除 accepted commit 或 append-only 事件来“修复”投影。
若只需要暂时关闭严格力量校验，可把 `power_system.mode` 切回 `shadow`，无需回退
accepted 账本。

## 8. 验收清单

- continuity schema 为 v5；
- replay 前后 accepted commit 数量和 canon revision 不变；
- replay 投影哈希稳定；
- `gain → use` 不丢失能力等级、成本与限制；
- `gain → lose → use` 被阻断；
- 旧能力可以按 owner 和 ability 双向查询；
- 未建 BridgeRule 的体系不发生数值换算；
- mundane 项目没有被生成伪境界链；
- v1 初始化项目仍可读取；
- v2 bundle hash 在同输入下稳定；
- secret scan、插件校验、全量测试和力量 benchmark 全部通过。
