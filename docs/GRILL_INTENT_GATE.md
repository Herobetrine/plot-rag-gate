# Grill 意图门禁

状态：已实现并在 `v1.4.3` 修复 fresh 剧情请求的显式 skip 与真实否定边界

协议：`plot-rag-intent/v1`

默认存储：`.plot-rag/grill.sqlite3`

最后更新：2026-07-17

## 1. 目的

Grill 位于“用户发起创作任务”和“剧情 prepare / 作品初始化”之间，用最少交互锁定本轮任务的真实目的、交付物、范围、成功标准和裁决边界。

它解决四类问题：

1. “剧情推演”“初始化一部作品”之类宽泛请求缺少可验收终点；
2. 模型在没有共享理解时直接生成，容易扩大范围或替用户决定核心设定；
3. 角色位置、道具、关系、力量状态、故事时间等项目事实本可通过 RAG 查询，却被重复问给用户；
4. “继续”“开始吧”等短续词在澄清阶段可能被误当成答案或剧情推进。

Grill 是**非正典控制层**。它不生成 accepted 事实，不替代项目 RAG，不签发 approval grant，也不改变 proposal、CAS、accepted commit 或 replay 语义。

## 2. 不可破坏的边界

- 项目事实与用户意图分离。角色状态、关系、位置、道具、力量状态和故事时间由 accepted RAG 读取，不进入 Grill 问卷。
- 每轮最多一个问题。当前问题未处理前，不追加第二个问题、方案、章纲、正文或初始化结果。
- 推荐答案只是建议；只有“按推荐答案”“你来定”等明确委托才视为采用。
- Grill 问答、inspect、repeat、cancel 都不创建剧情 receipt，不运行剧情 prepare，也不触发 Stop proposal 抽取。
- Intent Contract 只约束本轮任务，不具有正典权威。
- `.plot-rag/grill.sqlite3` 不属于 authority source，不进入长篇索引、InitializationBundle、连续性事件、proposal、accepted commit、snapshot 或 replay。
- 未完成、仅 handoff、取消或失败的会话不能被当作已达成共识。

## 3. Intent Contract

`plot-rag-intent/v1` 固定包含八个字段：

| 字段 | 含义 | 典型问题 |
| --- | --- | --- |
| `problem_to_solve` | 本轮真正要解决的创作问题 | 谁要达成什么，最大阻力是什么，结束时什么必须改变？ |
| `expected_deliverable` | 可直接验收的交付物 | 事件链、卷纲、章纲、场景、正文还是 InitializationBundle？ |
| `reader_experience` | 主体验与辅助体验 | 爽、悬念、压迫、治愈、热血或反转如何排序？ |
| `protagonist_drive_conflict` | 主角欲望、阻力与失败代价 | 主角为什么必须行动，对手如何阻止，失败失去什么？ |
| `scope_endpoint` | 起点、推进范围和停止点 | 推到哪次不可逆变化或哪个初始化目标档位？ |
| `success_criteria` | 完成后的可观察标准 | 哪些因果、选择、兑现和状态变化必须出现？ |
| `hard_constraints` | 正典、底线、禁区和保留项 | 哪些人物、视角、设定或情节绝对不能动？ |
| `model_autonomy` | 模型可自行裁决的空间 | 哪些细节可自由决定，哪些核心决定必须交还用户？ |

每个字段保存：

```json
{
  "value": "字段值",
  "source": "prompt|user_answer|recommended_delegation|workflow_default|quick_path_default|grill_skip_default|question_limit_default|unknown"
}
```

来源字段使合同中的用户原话、结构性默认值、快通道补全和 skip 补全可区分、可审计。

## 4. 默认行为

```json
{
  "grill": {
    "enabled": true,
    "schema_version": "plot-rag-intent/v1",
    "database_path": ".plot-rag/grill.sqlite3",
    "one_question_per_turn": true,
    "recommend_answer": true,
    "explore_project_first": true,
    "max_questions": 6,
    "session_ttl_seconds": 21600,
    "required_fields": [
      "problem_to_solve",
      "expected_deliverable",
      "reader_experience",
      "protagonist_drive_conflict",
      "scope_endpoint",
      "success_criteria",
      "hard_constraints",
      "model_autonomy"
    ]
  }
}
```

- `enabled=true`：新的剧情或初始化创作请求先进入 Grill。
- `one_question_per_turn=true`：每轮只处理一个上游依赖。
- `recommend_answer=true`：问题后附推荐答案和理由。
- `explore_project_first=true`：先探测项目配置、authority 规则、连续性库与初始化库；可检索事实留给后续 RAG。
- `max_questions=6`：问题上限。达到上限后，以 `question_limit_default` 补齐剩余字段并 handoff。
- `session_ttl_seconds=21600`：合同复用窗口为六小时；过期合同不用于 continuation。

`required_fields`、`skip_phrases` 和 `cancel_phrases` 可在项目 config v3 中覆盖。合同 schema 仍固定为 `plot-rag-intent/v1`。

## 5. 单问状态机

```text
无会话
  → 从 prompt 确定性提取合同字段
      ├─ 意图已明确
      │   → 补齐 quick_path_default
      │   → EXECUTING
      │   → handoff
      ├─ 明确 skip
      │   → 补齐 grill_skip_default
      │   → EXECUTING
      │   → handoff
      └─ 仍有关键缺口
          → AWAITING_ANSWER
          → 每轮一个问题
              ├─ 普通答案 → 写入 user_answer → 下一依赖
              ├─ 按推荐答案 / 你来定 → 采用当前推荐 → 下一依赖
              ├─ 继续 / 开始吧 / 下一步 → 重复当前问题
              ├─ 为什么问这个 / 还剩几题 → inspect，不推进
              ├─ skip → 补齐默认合同 → EXECUTING → handoff
              └─ cancel → CANCELLED

handoff 成功
  → COMPLETED

handoff 持久化失败
  → HANDOFF_FAILED
```

状态语义：

| 状态 | 是否占用问答 | 是否可被 continuation 复用 | 是否属于正典 |
| --- | --- | --- | --- |
| `AWAITING_ANSWER` | 是 | 否 | 否 |
| `EXECUTING` | 否，已交接 | 否 | 否 |
| `COMPLETED` | 否 | 是，需同项目、同宿主会话、同任务族且未过期 | 否 |
| `CANCELLED` | 否 | 否 | 否 |
| `HANDOFF_FAILED` | 否 | 否 | 否 |

## 6. 零问快通道

当输入至少明确：

- `problem_to_solve`
- `expected_deliverable`
- `scope_endpoint`

并在读者体验、主角冲突、成功标准、硬约束中覆盖至少两项时，Grill 不展示问题。剩余字段使用 `quick_path_default` 补齐，合同在同一轮交接。

零问不是关闭 Grill。它仍会：

1. 建立版本化 Intent Contract；
2. 记录字段来源；
3. 将 `[LOCKED_INTENT_CONTRACT]` 追加到执行 prompt；
4. 绑定 handoff turn，防止重复 prepare。

## 7. skip、cancel、repeat 与 inspect

默认 skip 语句包括：

- `跳过 Grill`
- `跳过盘问`
- `跳过目的确认`
- `按现有要求直接执行`
- `直接执行，不要追问`

skip 会用 `grill_skip_default` 补齐缺项并立即 handoff，不会把空字段伪装成用户原话。

默认 cancel 语句包括：

- `取消本轮 Grill`
- `结束本轮盘问`
- `停止本轮盘问`
- `放弃本轮任务`

cancel 结束当前 Grill，不执行原剧情或初始化任务。

严格短续词：

- `继续`
- `继续吧`
- `开始`
- `开始吧`
- `下一步`
- `接着来`
- `往下`

它们在 active Grill 中只重复当前问题，不改变 revision，不消费推荐答案。

inspect 语句如“为什么问这个”“还剩几题”“查看 Grill 状态”，只报告剩余字段并重复当前问题，不推进合同。

## 8. 项目先探测

`explore_project_first=true` 时，Hook 提供只读 project probe：

- 项目根路径；
- config 是否存在；
- authority source 规则数量；
- continuity store 是否存在；
- initialization store 是否存在。

该 probe 的目标是划分“可检索事实”和“必须由用户裁决的意图”：

| 内容 | 处理方式 |
| --- | --- |
| 角色当前位置、道具持有、关系、力量状态、故事时间 | 合同锁定后由 RAG 检索 |
| 本轮要解决什么、做到哪里、如何验收 | Grill 追问 |
| accepted 正典冲突 | prepare / validator 处理 |
| 用户是否允许新增核心设定 | Grill 的 `hard_constraints` / `model_autonomy` |

Grill 不把项目 probe 当成完整连续性查询，也不把“数据库存在”解释成某项事实存在。实际事实仍由 prepare 的 accepted 检索链决定。

## 9. Hook 顺序

`UserPromptSubmit` 固定顺序：

1. **活跃初始化会话优先**：真实初始化回答、继续、inspect、propose、apply 和 cancel 直接进入初始化；插件维护、分析、审查、测试、仓库和发布元任务不被当作答案。
2. **活跃 Grill 接管**：处理答案、repeat、inspect、skip 和 cancel。
3. **新创作请求进入 Grill**：剧情推演和首次初始化都先锁定合同。
4. **合同 handoff**：零问、完成或明确 skip 后，初始化进入 start，剧情进入 prepare。
5. **元问题静默**：插件说明、流程设计、插件维护、分析、审查、测试、仓库、发布、查询、否定和暂停不创建 Grill、初始化 answer 或剧情 receipt；讨论“剧情推演”的测试、关键词、正则、触发器或执行流程时同样保持静默。

新创作识别同时覆盖显式命令与自然网文表达，例如“剧情推演”“推演下一章”“续一章”“再来一章”“把下一章写出来”“接着上一章写”“规划本卷后半段”“把章纲扩成正文”。只有真实创作动作进入 Grill；引用这些说法询问“会不会触发”仍属于元问题。

`Stop` 固定顺序：

1. 活跃初始化会话抑制剧情抽取；
2. Grill 拥有的问答、repeat、inspect、cancel 或冲突 turn 抑制剧情抽取；
3. 已成功 handoff 的剧情 turn 执行 config v3 proposal 抽取；
4. 成功完成对应执行后，将 Grill 会话从 `EXECUTING` 标记为 `COMPLETED`。

同一 `turn_id` + 同一请求返回缓存响应；同一 `turn_id` 携带不同请求返回 conflict，并保持既有 Grill 状态不变。

## 10. 初始化接入

首次初始化请求：

```text
初始化一部作品
→ Grill
→ plot-rag-intent/v1
→ 零问 / 完成 / skip
→ initialization start
→ new / ingest / hybrid
```

专项规则：

- `expected_deliverable` 默认可为 `InitializationBundle 与标准作品骨架`；
- `scope_endpoint` 可选择 `plot_ready / world_bible / normalize_only / continuity_ready`；
- 完整题材、世界、剧情、整理范围与约束可以零问 handoff；
- Grill 的最多六问不替代初始化引擎内部的自适应决策包；
- 无 config 的新目录也能先建立 Grill，再在 handoff 时启动初始化；
- 初始化 session 一旦 active，优先级高于 Grill，后续不重复意图盘问；
- 首次 Grill 问答轮不创建初始化 session；只有合同 handoff 才启动初始化。

## 11. 持久化与故障边界

`.plot-rag/grill.sqlite3` 保存：

- `grill_sessions`：合同、状态、revision、TTL、当前问题、推荐答案与 handoff 绑定；
- `grill_turn_responses`：按 host session、project root、turn ID 保存幂等响应；
- `grill_meta`：数据库 schema version。

安全规则：

- 只读 active lookup 在数据库不存在时不创建文件；
- 未知数据库 schema 不被静默迁移或重写；
- Grill store 损坏或 lookup 失败时，相关 Stop fail-closed 地抑制剧情抽取；
- handoff 持久化失败进入 `HANDOFF_FAILED`，不会继续假装共识已完成；
- 删除 Grill 数据库只清除非正典澄清进度，不改变 continuity、init、proposal 或 accepted 数据。

## 12. 从 v1.3.0 升级

config v3 的迁移规则：

1. 缺失 `grill` 块时采用 v1.4.0 默认配置，Grill 默认启用。
2. 数据库按需创建；仅查询、元问题和 SessionStart 不应凭空创建 Grill store。
3. 已存在的 active initialization session 不迁移为 Grill session，继续拥有最高优先级。
4. v1.3.0 及更早的剧情 receipt、proposal、accepted commit、state schema 和 InitializationBundle 不需要因 Grill 改写。
5. continuation 只读取 v1.4.0 创建且状态为 `COMPLETED` 的合同，不从旧 transcript 猜造完整合同。

保持旧交互语义：

```json
{
  "grill": {
    "enabled": false
  }
}
```

关闭后，config v3 恢复直接剧情 prepare / 初始化仲裁；已有 Grill 数据保留但不参与执行。

## 13. 回滚

最小回滚：

1. 设置 `grill.enabled=false`；
2. 重启或开启新任务加载更新配置；
3. 验证新的剧情请求直接进入 prepare，新的初始化请求直接进入初始化仲裁。

完全清理非正典 Grill 状态时可删除 `.plot-rag/grill.sqlite3`。该删除不需要 continuity replay，也不应修改：

- `.plot-rag/state.sqlite3`
- `.plot-rag/init.sqlite3`
- accepted source manifest
- immutable commits
- 标准作品文件
- `BOOTSTRAP_READY`

不要通过手工修改 SQLite 行把 `EXECUTING` 或失败会话改为 `COMPLETED`。

## 14. 验收矩阵

必须覆盖：

- 模糊任务每轮一个问题、一个推荐答案和一个理由；
- 明确任务零问 handoff；
- 项目事实不成为意图问题；
- “按推荐答案”推进一个字段，“继续”只重复；
- inspect 不推进 revision，skip/cancel 语义独立；
- 问题上限后默认补齐并 handoff；
- 只复用未过期 `COMPLETED` 合同；
- `EXECUTING`、`HANDOFF_FAILED`、`CANCELLED` 和过期合同不复用；
- active init > active Grill > new creative request 的 Hook 顺序；
- Grill-owned Stop 零剧情 receipt、零 proposal、零状态写入；
- configless 初始化先 Grill 后启动；
- `grill.enabled=false` 兼容；
- 同 turn 幂等、不同请求 conflict；
- 只读 lookup 零创建、未知 schema 零改写；
- Grill 数据不进入 authority、InitializationBundle、accepted commit 或 replay。

量化门禁：

| 指标 | 目标 |
| --- | --- |
| 每轮 Grill 问题数 | `<= 1` |
| 模糊任务推荐答案数 | `1` |
| 可检索项目事实误追问 | `0` |
| 明确任务多余 Grill 轮次 | `0` |
| 短续词误消费答案 | `0` |
| 非 `COMPLETED` 合同复用 | `0` |
| Grill 问答轮剧情 receipt / proposal | `0 / 0` |
| Grill 控制状态进入正典 | `0` |
