# 网文作品初始化框架

状态：已完成（`plot-rag-init/v1` 与 `plot-rag-init/v2`，并接入插件 `v1.4.2` Grill、active-init 元任务隔离与备份恢复门禁）

当前协议：`auto` 协商 `plot-rag-init/v1` / `plot-rag-init/v2`

适用插件：`plot-rag-gate`

最后更新：2026-07-17

实现范围：默认启用的 `plot-rag-intent/v1` Grill、`new / ingest / hybrid`、四种目标档位、三种交互档位、初始化 hook 仲裁、只读 inventory、checkpoint/resume、冻结 proposal、grant-gated apply、materialize、verify、`COMPLETED` 与 `BOOTSTRAP_READY`，以及 v2 力量定义 proposal → 初始化运行态 proposal 的两阶段正典提交。

本文件继续冻结 `plot-rag-init/v1` 语义，并记录向后兼容的 `InitializationBundle v2`：通用项目保持 v1；出现结构化力量 claim、显式 profile 或力量对象时协商为 v2。v2 的统一内核、题材 adapter、两阶段 proposal 与迁移边界见 [POWER_SYSTEM_ADAPTATION_PLAN.md](POWER_SYSTEM_ADAPTATION_PLAN.md)，不原地改变 v1 schema 的含义。

## 1. 目标

初始化不是“一次生成一批设定文件”，而是把用户的创作意图或已有资料转换为一份可恢复、可审阅、可追踪来源、可重放的作品初始提交。

本框架包含两条来源路径、三种持久化模式：

```text
从零路径      new
已有内容路径  ingest / hybrid
```

三种模式的含义：

```text
new      从零按“题材 → 世界 → 剧情”创建作品
ingest   只读整理已有设定、正文、大纲和笔记，只报告缺口
hybrid   先执行 ingest，再只补真正阻塞下游的缺口
```

两条路径最终汇合到同一条安全链路：

```text
非正典初始化会话
→ InitializationBundle
→ 本地校验
→ 冻结 proposal
→ 宿主审批
→ accepted initialization commit
→ 标准文件与运行投影
→ COMPLETED + BOOTSTRAP_READY
```

初始化的最低目标是让作品达到“可以可靠规划总纲、第一卷和第一条事件链”的状态，而不是在动笔前填完一部世界百科。

### 1.1 术语与历史版本边界

| 术语 | 含义 | 最早版本 |
| --- | --- | --- |
| `proposal` | 冻结但未进入正典的初始化包 | v0.4.5 |
| approval grant 基础设施 | 可信宿主签发的一次性验收能力、通用存储与消费协议 | v0.4.0 |
| initialization grant binding | 把 grant 绑定到初始化 package、来源、目标文件和 materialize 权限 | v0.5 |
| accept transaction | 内部原子事务：CAS、消费 grant、记录请求哈希并创建 accepted commit | v0.5 |
| materialize | 从 accepted commit 生成和激活标准文件及派生投影 | v0.5 |
| `init apply` | 对外编排命令，依次执行 accept transaction 和可恢复 materialize | v0.5 |
| `COMPLETED` | 初始化 session 的终态 | v0.5 |
| `BOOTSTRAP_READY` | 项目级 readiness flag，表示初始化提交及必要投影均已验证 | v0.5 |

历史上的 v0.4.5 运行面只到 `PROPOSAL_FROZEN` 或 `AWAITING_APPROVAL`；v1.0.0 已包含 v0.5+ 的 apply、materialize、accepted commit、completion verify 和 `BOOTSTRAP_READY`。

## 2. 不可破坏的边界

1. 初始化过程中的用户回答、模型提案、来源分类和标准化结果均为非正典。
2. 硅基流动模型只负责分类、抽取、归一、补全候选和生成 proposal，不拥有文件、数据库或正典写权限。
3. `UserPromptSubmit`、`Stop` 和普通 MCP 调用无权签发 acceptance grant。
4. proposal 只有在消费宿主或交互式本地通道签发的一次性 approval grant 后，才可成为 accepted commit。
5. 在类型化事件、纠正、撤销和确定性重放完成前，不实现批量 `init apply`。
6. `new`、`ingest`、`hybrid` 在 proposal 阶段都不得改变：
   - `canon_revision`
   - accepted event 数量
   - `current_facts`
   - `timeless_facts`
   - accepted source manifest
   - authority config
7. 原始资料默认只读；初始化不得静默重命名、移动、覆盖或删除源文件。
8. 路径和文件名只提供来源角色候选，不能自动授予正典身份。
9. `unknown`、`conflicted` 和 `deferred` 是合法的一等状态，不得用模型猜测填平。
10. 世界客观真相、人物认知、社会公开叙事、读者已知信息和作者未来计划必须分层保存。
11. 初始化会话目录必须从剧情 hook、authority discovery、RAG index 和 Stop 状态抽取中排除。
12. 插件独立维护自身协议、schema 和存储，不导入或修改 `webnovel-writer` 的 `.webnovel`、数据库或 Python 模块。
13. Grill 合同与初始化 bundle 分离；`.plot-rag/grill.sqlite3` 只保存非正典任务意图、问题进度和 turn 幂等响应，不能成为初始化 claim、来源或 accepted 事实。

## 3. 初始化模式与目标档位

### 3.1 模式

| 模式 | 适用输入 | 默认行为 |
| --- | --- | --- |
| `new` | 空目录、灵感、一句话、题材名、角色种子 | 进入题材合同、世界因果核和剧情发动机 |
| `ingest` | 已有正文、设定、大纲、角色表、笔记 | 只读扫描、抽取、冲突整理和标准化 |
| `hybrid` | 已有资料不完整，同时希望继续共创 | 先 ingest，再按缺口进入相应 new 阶段 |

`auto` 是启动时的路由选择器，不写入 session 的 `mode` 字段。路由规则：

- 没有有效创作资料：`new`
- 存在正文、设定、大纲或角色资料：`ingest`
- 同时存在已有资料和明确的新创作要求：`hybrid`
- 用户显式指定的模式优先于自动判断

### 3.2 目标档位

| 档位 | 目标 |
| --- | --- |
| `plot_ready` | 默认；足以规划总纲、第一卷和第一条事件链 |
| `world_bible` | 展开更完整的世界运行模型和区域结构 |
| `normalize_only` | 只整理已有内容，缺项保持 `unknown` |
| `continuity_ready` | 面向已有连载，优先恢复正典、当前状态和剧情债务 |

模式描述“从哪里进入”，目标档位描述“初始化到什么程度”，二者相互独立。

合法组合：

| 模式 | `plot_ready` | `world_bible` | `normalize_only` | `continuity_ready` |
| --- | --- | --- | --- | --- |
| `new` | 支持 | 支持 | `PROFILE_MODE_MISMATCH` | `PROFILE_MODE_MISMATCH` |
| `ingest` | 支持，只报告缺口 | 支持，只报告缺口 | 支持 | 支持 |
| `hybrid` | 支持 | 支持 | 归一为 `ingest + normalize_only` | 支持 |

`ingest` 即使使用 `plot_ready` 或 `world_bible`，也只计算充分性和缺口，不自动进入补问。只有 `hybrid` 或用户显式批准模式转换后，才进入 new 路径的缺失节点。

## 4. 总状态机

```text
新的初始化请求
  → Grill: AWAITING_ANSWER
      ├─ 每轮一个问题 + 推荐答案
      ├─ 明确 skip → 默认合同
      ├─ 明确 cancel → CANCELLED，不启动初始化
      └─ 零问或合同完成 → EXECUTING
  → 初始化 handoff
  → Grill: COMPLETED

CREATED
  → DISCOVER
  → ROUTING
      ├─ NEW
      │   → GENRE_CONTRACT
      │   → WORLD_CAUSAL_KERNEL
      │   → ACTOR_ANCHOR
      │   → STORY_ENGINE
      │   → SERIALIZATION_CONTRACT
      ├─ INGEST
      │   → INVENTORY
      │   → CLASSIFY
      │   → EXTRACT
      │   → CONFLICT
      │   → GAP
      └─ HYBRID
          → INVENTORY
          → CLASSIFY
          → EXTRACT
          → CONFLICT
          → GAP
          → 按硬缺口进入 NEW 的对应阶段
  → NORMALIZE
  → VALIDATE
  → REVIEW
  → READY_TO_PROPOSE
  → PROPOSAL_FROZEN
  → AWAITING_APPROVAL

v0.5+:
  AWAITING_APPROVAL
  → APPLYING
  → VERIFYING
  → COMPLETED
  → project.BOOTSTRAP_READY = true
```

旁路状态：

| 状态 | 含义 |
| --- | --- |
| `NEEDS_INPUT` | 当前存在必须由用户裁决的阻塞项 |
| `PAUSED_REMOTE` | 硅基流动超时、限流、空响应或 schema 校验失败 |
| `STALE_SOURCE` | 冻结后源文件内容哈希发生变化 |
| `STALE_CANON` | 现有项目的 `canon_revision` 已推进 |
| `SUPERSEDED` | 上游答案变化使旧 proposal 失效 |
| `CANCELLED` | 会话终止，正典保持原值 |

每个阶段必须具有：

- 明确输入；
- 确定性或可校验的转换；
- 退出条件；
- 非正典检查点；
- 可追踪的依赖节点；
- 失败后可恢复的下一动作。

## 5. 会话隔离与触发仲裁

### 5.1 会话目录

新作品：

```text
<WORKSPACE_ROOT>/.plot-rag-init/<session_id>/
```

已有项目：

```text
<PROJECT_ROOT>/.plot-rag/init-sessions/<session_id>/
```

这些目录只能保存初始化过程状态，不属于 authority source，也不参与剧情事实检索。

### 5.2 会话优先级

一次用户输入只能由一个主工作流消费：

```text
活跃初始化会话
→ 活跃 Grill
→ 新初始化或剧情创作请求先进入 Grill
→ Grill 完成 / 零问 / skip 后 handoff
→ 活跃 proposal 审阅或验收
→ 查询、分析、插件设计和其他元任务
```

当存在活跃初始化会话时：

- “继续”推进初始化状态机；
- “就按这个”“选第二个”“世界规则改成……”作为初始化答案处理；
- “查看冲突”“还有什么缺口”进入初始化 inspect；
- 不创建剧情 `receipt_id`；
- 不执行 plot progression Stop 抽取；
- 不把初始化设计文本记录为角色、关系、位置、道具或故事时间。

不存在活跃初始化会话但存在 active Grill 时：

- “继续”“开始吧”“下一步”只重复当前问题，不消费答案；
- “为什么问这个”“还剩几题”只 inspect 当前状态，不推进；
- “按推荐答案”“你来定”采用当前推荐并只推进一个依赖；
- 明确 skip 补齐默认合同并在同一轮启动初始化；
- 明确 cancel 只终止 Grill，不创建初始化 session；
- Grill 问答输出的 `Stop` 抑制剧情 proposal 抽取。

不存在活跃初始化或 Grill 时，短继续才参考最近已确认任务分类。插件设计、流程设计、初始化框架讨论中的“继续”不得被单句触发器误判为剧情推进。

### 5.3 v1.4.0 Grill 初始化接入

首次“初始化一部作品”“创建新作”“整理现有作品”等请求先进入 `plot-rag-intent/v1`。合同包含：

1. `problem_to_solve`
2. `expected_deliverable`
3. `reader_experience`
4. `protagonist_drive_conflict`
5. `scope_endpoint`
6. `success_criteria`
7. `hard_constraints`
8. `model_autonomy`

初始化专项规则：

- `expected_deliverable` 可使用工作流默认值 `InitializationBundle 与标准作品骨架`；
- `scope_endpoint` 的推荐答案使用 `plot_ready`，也允许用户明确选择 `world_bible / normalize_only / continuity_ready`；
- 先探测项目配置、authority 规则、连续性库和初始化库。已有角色、设定、位置、道具、力量状态和故事时间属于 ingest/RAG 输入，不作为 Grill 事实题；
- 足够具体的题材、整理目标、范围、约束和验收标准可走零问快通道；
- Grill 的 `max_questions=6` 是意图合同上限，不替代初始化内部的自适应题材/世界/剧情决策包；
- Grill handoff 成功后状态才进入 `COMPLETED`。初始化启动失败或 handoff 持久化失败时，合同不作为后续 continuation 共识；
- 初始化 session 一旦 active，即获得最高优先级；后续回答、继续、inspect、propose、apply 和 cancel 直接进入初始化状态机，不重复 Grill。

从 v1.3.0 升级：

1. config v3 缺失 `grill` 块时使用默认启用配置；新初始化请求首次命中时按需创建 `.plot-rag/grill.sqlite3`。
2. 现有 active 初始化 session 不迁移到 Grill，也不改变 session revision、bundle hash 或 canon revision。
3. 需要保持旧版“请求后直接启动初始化”体验时设置 `"grill": {"enabled": false}`。
4. `.plot-rag/grill.sqlite3` 可直接删除以清空意图会话；删除不改变 init session、proposal、accepted commit、标准文件或 `BOOTSTRAP_READY`。
5. 回滚只关闭 Grill 或删除其非正典数据库，不手工修改 `init.sqlite3` 或连续性数据库。

## 6. 初始化会话模型

建议的非正典会话对象：

```json
{
  "schema_version": "plot-rag-init/v1",
  "session_id": "init-...",
  "mode": "new|ingest|hybrid",
  "target_profile": "plot_ready",
  "stage": "WORLD_CAUSAL_KERNEL",
  "status": "NEEDS_INPUT",
  "workspace_root": "PATH",
  "project_root": "PATH",
  "session_revision": 7,
  "expected_canon_revision": 12,
  "source_snapshot_hash": "SHA256",
  "answers": {},
  "unknowns": [],
  "conflicts": [],
  "decisions": [],
  "dependency_graph": {},
  "created_at": "RFC3339",
  "updated_at": "RFC3339"
}
```

所有用户回答和结构化字段使用公共信封：

```json
{
  "value": {},
  "field_status": "user_confirmed|source_supported|model_proposed|unknown|conflicted|deferred|not_applicable",
  "origin": "user_input|source_extract|model_suggestion|deterministic_derived",
  "decision_status": "open|session_locked|delegated",
  "canon_status": "proposed|accepted|rejected|retracted",
  "source_refs": [],
  "confidence": 1.0,
  "scope": "current|planned|historical|timeless|null",
  "knowledge_plane": "objective|actor_belief|public_narrative|reader_disclosed|author_plan",
  "branch_id": "main",
  "depends_on": [],
  "invalidates": []
}
```

状态和来源必须分开。一个值可以同时是：

```text
field_status=user_confirmed
origin=model_suggestion
canon_status=proposed
scope=planned
```

这表示模型提出了候选、用户在初始化会话中明确选中、但它仍是尚未经过 acceptance grant 的未来规划。

## 7. InitializationBundle v1

两条来源路径最终归一为同一逻辑结构：

```yaml
initialization_bundle:
  meta:
  genre_contract:
  world_model:
  actor_system:
  story_engine:
  serialization_contract:
  entities:
  relations:
  timeline:
  open_loops:
  field_states:
  source_manifest:
  source_ownership:
  conflicts:
  gaps:
  decisions:
  provenance:
  artifact_manifest:
  validation:
```

### 7.1 模块职责

| 模块 | 内容 |
| --- | --- |
| `meta` | 协议版本、作品 ID、模式、目标档位、会话和 revision |
| `genre_contract` | 题材发动机、读者承诺、调性、规模和边界 |
| `world_model` | 世界规则、资源、行动者、压力和因果依赖 |
| `actor_system` | 主角、主动对手、关键第三方及其初始锚点 |
| `story_engine` | 触发事件、目标、阻力、代价、升级机制和终局问题 |
| `serialization_contract` | 章级反馈、兑现窗口、卷级循环和节奏约束 |
| `entities` | 稳定实体 ID、类型、名称和别名 |
| `relations` | 类型化关系边及其公开/私下状态 |
| `timeline` | 故事时间锚点、历史残留、计划与当前事件 |
| `open_loops` | 谜团、承诺、债务、期限和待兑现钩子 |
| `field_states` | 每个字段的确认、来源、置信度和 scope |
| `source_manifest` | 原始来源、内容哈希、角色、等级和纳入策略 |
| `source_ownership` | 每个结构化路径的唯一主写入来源 |
| `conflicts` | 冲突类型、候选解释和裁决 |
| `gaps` | 按目标档位计算的硬缺口与可延后缺口 |
| `decisions` | 用户选择、委托选择和变更历史 |
| `provenance` | 逐字证据、行号、模型、prompt 和响应哈希 |
| `artifact_manifest` | 计划生成的文件、旧哈希、新哈希和逻辑 owner |
| `validation` | schema、引用、不变量、充分性和压力测试结果 |

### 7.2 数据所有权

- accepted initialization commit 是结构化初始事实的权威记录。
- Markdown、JSON 快照、索引和摘要是可重建投影。
- 同一 schema path 只能有一个 owner。
- 多个来源支持同一事实时合并 evidence refs，不复制事实所有权。
- 原始来源仍是证据，不因生成标准文件而被删除。
- bundle 被接受不代表其中所有内容都进入 `current`：世界硬规则进入 `timeless`，开局现状进入 `current`，未来事件进入 `planned`，历史前因进入 `historical`，题材与连载合同留在创作控制层，人物认知进入独立 belief 投影。

### 7.3 InitializationBundle v2 与力量模型

初始化配置可使用 `schema_version=auto`。当 dossier、seed 或来源 claim 包含显式
power profile、`ability.* / progression.* / resource.*` 等谓词，或结构化力量对象时，
会话协商为 `plot-rag-init/v2`；其余项目继续使用 v1。

v2 保留 v1 全部字段，并增加：

```yaml
power_systems:
progression_tracks:
rank_nodes:
rank_edges:
ability_definitions:
resource_definitions:
counter_rules:
bridge_rules:
actor_power_bootstrap:
power_model:
```

`power_model` 使用 `plot-rag-power/v1`。初始化只根据用户输入和可追溯 claim 建模，
不依据题材惯例补造境界顺序、资源换算或克制关系。`mundane` profile 明确表示当前
作品没有超凡等级链。

定义平面与运行平面分别晋升：

- 体系、成长轨、阶段、晋升边、能力、资源、克制和桥接形成隔离
  `power_spec_package`；
- `power_spec_package` 使用 `proposal_kind=power_spec_change` 和
  `accept_power_spec` grant；
- 角色初始境界、能力持有、资源、状态、绑定和资格进入 initialization runtime events；
- 两类 proposal 分别审阅、分别消费 grant，并按 canon revision 顺序应用。

若另一个初始化 saga 已接受语义完全相同且仍 active 的 PowerSpec，运行时按稳定
`power_package_hash` 复用该定义提交，不重复消费 PowerSpec grant；新的
initialization runtime proposal 始终在**当前 active canon revision** 上准备并执行
自己的 CAS。binding 分别保存已接受 PowerSpec 的真实外层 `package_hash`、当前
saga 请求的 `requested_package_hash` 和稳定 `power_package_hash`。completion
receipt 通过 `power_spec_reused`、`power_spec_grant_consumed_in_this_saga`、
`initialization_grant_consumed` 及各自 revision 字段区分“本 saga 的消费”与
“累计接受链”，中间存在无关 canon commit 时也不伪造相邻 revision。

## 8. 从零入口：题材 → 世界 → 剧情

### 8.1 DISCOVER：种子与路由

输入可以只有：

- 一个题材；
- 一句话灵感；
- 一个角色；
- 一个场景；
- 一个书名；
- 一段已有构想。

系统先抽取：

- 用户已经明确的事实；
- 硬约束和禁区；
- 可复用候选；
- 当前冲突；
- 可以安全留空的字段；
- 初始化目标档位。

退出条件：

- workspace 和候选 project root 已确定；
- 模式和目标档位已确定；
- 原始种子保存进 session journal；
- canon guard 基线已记录。

### 8.2 GENRE_CONTRACT：题材合同

题材合同回答“读者为什么持续阅读”，而不只记录分类标签。

最小字段：

```yaml
genre_contract:
  primary_engine:
  secondary_engines: []
  target_readers:
  platform_assumptions:
  reading_promise:
  recurring_rewards: []
  differentiators: []
  tone:
  scale_expectation:
  pacing_expectation:
  hard_boundaries: []
  anti_promises: []
```

题材门禁：

- 有一个明确的主类型发动机；
- 辅助发动机最多两个且不互相抵消；
- 至少一个可持续兑现的读者承诺；
- 至少一个差异化变量；
- 已知调性、规模和明确排除项；
- 后续世界设计知道需要持续制造哪类压力、回报和升级。

信息稀疏时，系统生成两至三套差异化题材合同，每套说明对世界和剧情的实际影响，只请求一次选择。

### 8.3 WORLD_CAUSAL_KERNEL：世界因果核

默认先问三个高杠杆问题：

1. 世界中哪条规则几乎不可绕过？
2. 哪种资源、资格、能力或信息最稀缺？
3. 谁维护规则、谁获益、谁受损？

系统据此展开候选因果链：

```text
核心异常或规则
→ 资源与能力边界
→ 生产、交通和信息条件
→ 权力与利益分配
→ 阶层、文化和日常生活
→ 历史残留与当前压力
→ 人物可行行动
→ 自然生成的剧情冲突
```

世界因果核门禁：

- 一至三条不可随意违背的基础规则；
- 起点地点和最小空间连通图；
- 一条维持生存的资源链；
- 一条决定权力分配的稀缺资源链；
- 统治者、挑战者、服务者和承受后果的群体；
- 阶层、身份和至少一种上升或坠落通道；
- 当前仍生效的一条历史因果；
- 故事时钟或历法锚点；
- 至少两种阶层或生计的普通一天；
- 规则的成本、边界、痕迹、反制和例外协议；
- 近期、卷级和全书级压力至少各一个；
- 世界规则能够真实阻断人物最自然的解决方案。

### 8.4 ACTOR_ANCHOR：人物在世界中的锚点

人物不是脱离世界单独生成的简历，而是世界压力转化为剧情行动的接口。

每个核心行动者至少记录：

```yaml
actor_anchor:
  entity_id:
  identity:
  location:
  social_position:
  immediate_need:
  external_goal:
  long_term_desire:
  internal_lack:
  values_and_limits:
  capabilities: []
  resources: []
  debts: []
  relationships: []
  knows: []
  suspects: []
  misunderstands: []
  secrets: []
  default_strategy:
  offscreen_plan:
  world_blocker:
```

最小行动者集合：

- 主角；
- 主动对手；
- 至少一个受影响第三方或利益群体。

人物锚点门禁：

- 身份、位置、目标、资源和知识边界明确；
- 对手在主角不行动时仍会推进自己的计划；
- 主角采取最自然行动时会撞上世界规则、利益结构或信息障碍；
- 关系不是单一“朋友/敌人”标签，而是可独立变化的信任、情感、债务、权威、依赖和信息权限。

### 8.5 STORY_ENGINE：剧情发动机

最小结构：

```yaml
story_engine:
  protagonist:
  actionable_goal:
  inciting_event:
  active_opposition:
  stakes:
  failure_cost:
  world_constraints: []
  information_asymmetry:
  first_event_chain:
  escalation_loop:
  irreversible_state_changes: []
  volume_one_change:
  endgame_direction:
  endgame_question:
```

剧情发动机门禁：

- 外在目标可判断成败；
- 对手会主动改变局面，而不是等待主角触发；
- 失败代价具体；
- 第一条事件链有明确触发点；
- 世界规则会造成预期外结果；
- 每次阶段性成功都会改变资源、关系、身份、认知、位置或敌我格局；
- 第一卷结束时世界状态与开局不同；
- 题材承诺、世界压力和主角行动指向同一主线。

### 8.6 SERIALIZATION_CONTRACT：连载兑现合同

```yaml
serialization_contract:
  chapter_feedback_loop:
  recurring_reward_types: []
  tension_cycle:
  reveal_policy:
  hook_policy:
  promise_windows: []
  growth_accounts: []
  volume_loop:
  repetition_limits: []
  pacing_guardrails: []
```

该合同不规定每章机械套模板，只回答：

- 读者多久获得一次什么类型的回报；
- 哪些承诺必须在什么窗口兑现；
- 升级消耗什么资源并留下什么新问题；
- 每卷如何形成“目标—阻力—变化—新局面”的闭环；
- 哪些冲突、反转和爽点不能重复换皮。

## 9. 一个“真实的虚拟世界”包含什么

### 9.1 核心定义

```text
世界_t
= 坐标系
+ 规则
+ 当前状态
+ 行动者认知
+ 历史残留
+ 当前压力
```

```text
世界_t+1
= Apply(世界_t, 行动事件, 延迟后果)
```

行动者只依据自己掌握的信息行动：

```text
行动_i
= Decide(目标_i, 能力_i, 资源_i, 认知_i, 可见机会_i)
```

可信世界必须满足：

- 没有主角时仍会运转；
- 人物和组织会在场外继续行动；
- 移动、生产、通信和执法有时间、容量和成本；
- 强大能力有来源、边界、反制、痕迹和社会后果；
- 优势会被学习、垄断、产业化、管制或针对；
- 制度、边界、仇恨和利益有历史来源；
- 普通人能在其中完成一天的生活；
- 问题解决后留下利益重排、债务、创伤或新压力；
- 可以进行反事实推演。

### 9.2 九类底层对象

| 对象 | 含义 |
| --- | --- |
| `Coordinate` | 时间、地点、空间层级、路线、距离和历法 |
| `Rule` | 自然、超凡、技术和制度规则 |
| `Stock` | 人口、粮食、能源、财富、灵气、信用等存量 |
| `Flow` | 人、物、信息、能量、货币和命令的流动 |
| `Actor` | 人物、家庭、组织、阶层、国家和非人行动者 |
| `Relation` | 亲缘、所有权、信任、债务、隶属、敌对和通行关系 |
| `Belief` | 某行动者知道、相信、怀疑、误解或隐瞒的内容 |
| `Event` | 满足前提后改变世界状态的动作与反应 |
| `Pressure` | 需求、目标和现实容量之间的持续差距 |

复杂世界模块都应归一为这些对象和它们之间的边。

### 9.3 十三个运行模块

初始化按题材和当前卷分辨率选择性展开：

1. 时空坐标、路线、旅时和通信延迟；
2. 自然与超凡法则；
3. 资源与生态承载；
4. 人口、家庭、生计和迁徙；
5. 技术、生产和基础设施；
6. 制度、法律、暴力与权力；
7. 经济、产权、税收、债务和物流；
8. 文化、身份、宗教、礼仪和禁忌；
9. 知识、信息传播、保密和失真；
10. 历史惯性、未结旧账和集体记忆；
11. 行动者目标、能力、认知和场外计划；
12. 不同阶层的日常生活；
13. 压力梯度、触发阈值和冲突生成。

### 9.4 规则记录

每条关键世界规则至少包含：

```yaml
rule:
  statement:
  domain:
  applies_to: []
  preconditions: []
  mechanism:
  inputs: []
  outputs: []
  cost: []
  latency:
  capacity:
  limits: []
  failure_modes: []
  counters: []
  exceptions:
    - explained_by_rule_id:
  observable_signatures: []
  known_by: []
  maintainers: []
  beneficiaries: []
  harmed_groups: []
  systemic_effects: []
  generated_pressures: []
```

“特殊情况下可以”不是有效例外，例外必须由另一条规则解释。

### 9.5 世界分辨率

| 层级 | 内容 | 初始化策略 |
| --- | --- | --- |
| `kernel` | 会反复约束全书的规则与压力 | 初始化时明确 |
| `regional` | 国家、地域、势力和大规模资源流 | 当前卷需要时展开 |
| `local` | 城市、组织、供应链和人物网络 | 即将进入时展开 |
| `scene` | 街道、房间、商店和现场人物 | 场景前生成并确认 |
| `texture` | 不改变行动的风俗和装饰 | 延迟创建 |

初始化只强制完成 `kernel`。可逆、低影响的细节保持 `unknown` 或 `deferred`。

## 10. 最小可运行世界 MVW

`plot_ready` 的世界最低交付：

1. 一个故事时钟或历法锚点；
2. 三个与主线有关的地点及路线、旅时；
3. 一至三条基础规则；
4. 一个核心能力及其成本、边界和反制；
5. 一条生存资源链；
6. 一条权力稀缺资源链；
7. 两种阶层或生计的日常循环；
8. 一个交通、通信或生产瓶颈；
9. 一个正式制度及其发现、报告、裁决和执行链；
10. 两个利益冲突的权力行动者；
11. 一个承受后果的群体；
12. 一套维持合法性的文化叙事或禁忌；
13. 一个重要秘密及传播障碍；
14. 一次仍影响现在的历史创伤；
15. 近期、卷级和全书级压力；
16. 一个使当前平衡不可逆变化的触发事件。

MVW 的验收问题：

- 普通人今天怎样活下去？
- 谁控制关键资源，凭什么？
- 人、物和消息多久能抵达？
- 某人违规后谁会知道，谁来处理？
- 主角什么都不做，七天或三十天后会发生什么？
- 对手采取最合理行动时，世界如何反应？
- 眼前问题解决后，哪些状态不会恢复原状？

## 11. 自适应提问

初始化 schema 应实现为依赖图，不向用户一次展示完整问卷。

问题优先级：

```text
question_priority
= downstream_impact
× uncertainty
× late_change_cost
× user_only_knowledge
÷ answer_cost
```

实现时可以归一为：

```text
priority
= hard_blocker * 1000
+ conflict_severity * 100
+ downstream_dependency_count * 20
+ canon_risk * 10
- safe_defaultability * 5
- estimated_user_effort
```

交互规则：

- 每轮最多一至三个彼此相关的问题；
- 先问会解除最多下游阻塞的问题；
- 上游决策完成后才展示依赖它的下游问题；
- 优先给出从用户资料中抽出的候选，再给模型建议；
- 每个选项说明对世界与剧情的实际影响；
- 可安全留空的字段直接标记 `unknown` 或 `deferred`；
- 用户自然语言回答先归一为 patch，再回显短差异；
- 用户说“你来定”时记录 `delegated_choice=true`；
- 用户修改上游答案时，只失效依赖节点；
- `question_id + expected_session_revision` 防止过期回答；
- 已回答字段只在源文件变化或新冲突出现时重新进入队列。

停止追问条件：

- 当前目标档位的硬门禁全部通过；
- 余下字段可安全延后；
- 再问问题的预期信息增益低于回答成本；
- 已能生成可审阅 proposal。

完整 seed 应实现零次内容追问。只有题材的稀疏 seed，默认最多经过题材、世界、剧情三个决策包后进入 proposal 审阅。

## 12. 已有内容入口

### 12.1 INVENTORY：只读发现

用户显式指定 source roots 后，记录：

- 相对路径和解析后的真实路径；
- 大小、mtime、SHA-256；
- 编码、格式和可解析状态；
- 重复文件组；
- 正文、设定、大纲、草稿、笔记、TODO 和参考资料候选；
- 排除原因和读取问题。

默认排除：

- `.git`
- `.plot-rag/init*`
- `.plot-rag-init`
- 缓存、日志、备份和构建产物
- 已生成的 index、snapshot、projection 和 commit
- source root 之外的 junction 或 symlink 目标

inventory 前后源文件字节和哈希必须一致。

### 12.2 CLASSIFY：来源分类

每个来源获得独立维度：

```yaml
source_descriptor:
  source_id:
  path:
  real_path:
  content_hash:
  source_role: canon|setting|outline|draft|note|reference
  authority_tier: T0|T1|T2|T3|T4|T5
  artifact_stage:
  scope_policy: infer_and_review|planned_only|timeless_candidate|preserve_unknown
  ingest_policy: include|review|exclude
  branch_id:
  chapter_hint:
  priority:
  classification_confidence:
```

来源等级：

| Tier | 来源 | 初始化作用 |
| --- | --- | --- |
| `T0` | accepted commit 和事件账本 | 现有项目最高基线 |
| `T1` | 作者确认的 final/published 正文或 setting | 可支持 current、historical 或 timeless |
| `T2` | 已确认 outline/plan | 只支持 planned |
| `T3` | working draft、章纲、未定设定 | branch/provisional |
| `T4` | 灵感、聊天片段、TODO、随手笔记 | 线索和 gap 候选 |
| `T5` | 外部作品、研究资料、AI 摘要、模型推断 | craft/reference |

规则：

- 路径名只能生成候选分类；
- 影响正典的低置信分类进入用户批量裁决；
- `T5` 不能单独支撑项目事实；
- TODO 不因勾选状态自动晋升；
- 模型建议经用户接受后保留 `origin=model_suggestion`。

### 12.3 EXTRACT：类型化 claim

抽取范围：

- 实体、别名和 mention；
- 世界规则与例外；
- 角色状态、目标、认知、能力和身份；
- 关系、地点、移动、道具和资源；
- 时间锚点和事件；
- 势力、制度与权力结构；
- open loop、伏笔、承诺和期限；
- 题材合同和剧情发动机。

每条 claim 必须保存：

```yaml
claim:
  claim_id:
  subject:
  predicate:
  object_or_value:
  exact_evidence:
  path:
  line_start:
  line_end:
  source_hash:
  support_type: exact|paraphrase|inference
  source_role:
  authority_tier:
  field_status:
  canon_status: proposed|accepted|rejected|retracted
  origin: source_extract|model_suggestion|deterministic_derived
  scope:
  knowledge_plane:
  modality: asserted|hypothetical|conditional
  branch_id:
  story_time:
  extraction_model:
  prompt_version:
  response_hash:
  confidence:
```

`inference` 只能进入候选层。没有逐字证据的模型补全不得伪装为来源支持事实。

### 12.4 实体与别名归一

归一时必须区分：

- 同一人物的姓名、称号、昵称和代词；
- 同名异人；
- 同名异物；
- 组织与其负责人；
- 道具类型和具体唯一实例；
- 地点名称和行政辖区；
- 关系端点。

低置信解析保持 `AMBIGUOUS`，不得因为向量相似就合并实体。

### 12.5 时间、分支和信息层隔离

所有 claim 必须尝试归入 accepted scope 候选：

```text
current
planned
historical
timeless
```

假设内容使用 `modality=hypothetical`、保持 `canon_status=proposed` 并进入显式 branch；时间含义不明的内容保持 `field_status=unknown` 且 `scope=null`。二者都不能伪装成第五或第六种 accepted scope。

还必须区分：

- 客观事实；
- 人物相信或误解的内容；
- 社会公开说法；
- 读者已经得知的内容；
- 作者未来计划。

`knowledge_plane` 与 `scope` 正交：作者计划可以描述 `historical` 背景，人物误解也可以针对 `current` 状态；不得把 `author_plan` 自动等同于 `planned`。

正文叙述顺序不等于故事发生顺序。没有可靠故事时间时保留 `unknown`。

### 12.6 CONFLICT：冲突图

冲突类型：

- 重复事实；
- 别名或实体歧义；
- 真实时间演化；
- 插叙或并发场景；
- 备选分支；
- planned/current 混淆；
- 版本替换；
- 明确语义矛盾；
- 同名异人或同名异物；
- 来源角色冲突。

裁决操作：

```text
choose_source
temporalize
merge_alias
split_entity
assign_branch
supersede
retract
author_value
defer_and_exclude
```

规则：

- 每个冲突先分类，再决定是否需要用户回答；
- 核心合同冲突必须裁决；
- deferred 冲突对应 claim 不进入 accepted delta；
- 现有 T0 正典保持有效，改变必须表现为 correction、supersession 或 retraction proposal。

### 12.7 GAP：缺口矩阵

缺口按目标档位和下游影响计算：

1. 阻塞题材合同；
2. 阻塞世界因果核；
3. 阻塞人物锚点；
4. 阻塞剧情发动机；
5. 阻塞连续性恢复；
6. 可安全延后的百科细节。

`ingest` 无论目标档位为何，都只保存 gap report 并继续 normalize，不自动补问。`hybrid` 才按 `plot_ready / world_bible / continuity_ready` 的硬缺口进入对应 new 节点；用户也可以显式批准把当前 ingest session 转换为 hybrid。`normalize_only` 始终保留缺口原状。

### 12.8 NORMALIZE：标准化

标准化产物必须具备：

- 稳定实体 ID 和 alias；
- 结构化故事时间；
- scope、branch 和 artifact stage；
- 来源角色和 authority tier；
- 唯一 source ownership；
- 世界模型、人物锚点和剧情合同；
- 原始文件到标准结构的映射；
- 目标文件清单和逐文件 diff；
- unknown、deferred 和 conflicted 的无损保留。

### 12.9 DRY-RUN 与 proposal

本文中的 bootstrap 表示“把作品初始化为可运行正典”的生命周期。公开 CLI 统一使用 `init` 命名空间，不再维护一套含义重复的 bootstrap 命令。

```text
init dry-run
→ one-shot 读取 seed/source 并在内存中完成发现、归一和校验
→ stdout 或用户显式指定的报告
→ 零 session、零数据库、零 canon、零默认文件写入

init inspect
→ 只读查看已经显式创建的非正典 session
→ 不推进状态机、不写 session journal

init propose
→ 保存非正典、不可变 proposal
→ 不更新 accepted event、current projection 或 canon revision
```

冻结 proposal 至少包含：

- source manifest；
- 用户回答和决策；
- normalized dossier；
- conflict resolutions；
- unknown 和 deferred；
- artifact manifest；
- 逐文件 diff；
- proposed canon deltas；
- apply plan；
- validation report；
- package hash。

## 13. 来源证据与裁决

### 13.1 证据要求

所有来源支持的关键事实必须能够回到：

- 精确路径；
- 行号或结构化路径；
- 内容哈希；
- 连续逐字证据；
- source role 和 tier；
- scope、branch 和 story time。

模型生成候选必须保存：

- 模型名；
- prompt 版本；
- 请求 schema；
- 响应哈希；
- 本地 validator 版本。

不得保存 API key。

### 13.2 冲突优先级

默认裁决顺序：

```text
accepted T0
→ 用户当前明确裁决
→ T1 final/published
→ T2 accepted outline
→ T3 working material
→ T4 note/TODO
→ T5 external/model inference
```

该顺序只决定默认候选，不代替用户裁决，也不能用来覆盖时间演化、分支差异或人物误解。

### 13.3 原始资料保护

- 原始目录只读；
- 标准化文件写到 staging；
- 每个目标文件保存 expected old hash 和 proposed new hash；
- apply 前重新验证源哈希和目标哈希；
- 冲突时停止并重新生成 proposal；
- 不使用“整理”作为删除原文件的理由。

## 14. 标准文件投影

逻辑结构优先于固定中文目录名。默认可读投影建议如下：

```text
<PROJECT_ROOT>/
  作品合同/
    题材合同.md
    连载兑现合同.md
  设定集/
    世界内核.md
    时空与地理.md
    规则与力量.md
    资源与社会.md
    历史与当前压力.md
  角色/
    角色索引.md
    <entity-id>.md
  剧情/
    故事发动机.md
    总纲.md
    未决剧情债务.md
  正文/
  资料/
  .plot-rag/
    config.json
    accepted-source-manifest.json
    commits/
    projections/
```

投影规则：

- 目录名可以配置，schema path 和稳定 ID 不随文件名变化；
- 标准 Markdown 是人类可读投影，不是唯一数据库真相；
- 每个结构化字段只有一个 owner 文件；
- 其他文件通过引用或摘要使用该字段；
- 生成前提供逐文件 diff；
- 已有文件默认不覆盖，只有 approval grant 绑定的精确 old/new hash 可以激活；
- 用户选择保留原结构时，只生成映射和缺口报告，不强制搬家；
- accepted source manifest 只绑定已批准路径和内容哈希；
- RAG 不因文件刚生成就自动把半成品视为正典。

## 15. approval 与 apply（已实现）

approval grant 至少绑定：

```yaml
approval_grant:
  approval_id:
  proposal_id:
  package_hash:
  artifact_stage: bootstrap
  branch_id: main
  chapter_no: null
  artifact_revision:
  target_project_real_path:
  source_manifest_hash:
  target_old_new_hashes: []
  authorized_operations:
    - accept_initialization
    - materialize
  authorized_paths: []
  expected_canon_revision:
  issuer:
  channel:
  expires_at:
  token_hash:
  consumed_request_hash: null
  accepted_commit_id: null
```

grant 只能由可信宿主或交互式本地通道签发。非交互 CLI、MCP、`UserPromptSubmit`、`Stop` 和硅基流动模型都只能提交 proposal 或消费已有 grant，没有签发权限。本地服务负责校验、消费和审计 grant，不负责自行赋予授权。

apply 顺序：

1. 校验 grant、proposal、target root、source hash、target hash 和 canon revision；
2. 获取初始化锁；
3. 把文件生成到 staging；
4. 对 staging 执行 schema、引用、路径、secret 和结构验证；
5. 计算绑定 proposal、grant、目标哈希和 canon revision 的 accepted request hash；
6. 在单一数据库事务中重新校验 CAS，记录 accepted request hash，消费 grant，创建 immutable initialization commit，推进 canon revision，并把 materialization plan 标记为 pending；
7. 以原子 rename/replace 激活 staging 中的标准文件；
8. 在独立短事务中核对实际文件哈希，激活 accepted source manifest 的文件绑定，并把 materialization 标记为 ready；
9. 生成 state、index、snapshot、summary 和 vector 投影；
10. 验证投影哈希并写 completion receipt；
11. 把 session 标记为 `COMPLETED`，并设置项目 `BOOTSTRAP_READY=true`。

apply 必须是可恢复工作流。若第 6 步后崩溃，相同 accepted request hash 的重试返回原 commit，并从 materialize 检查点继续；不同请求复用已消费 grant 必须失败。标准文件尚未激活或哈希未验证时，accepted manifest 中对应文件保持 pending，RAG 通过 accepted commit 和旧的 active manifest 工作，不读取半成品。

## 16. CLI 命令合同

`init start` 必须能在项目尚无 `.plot-rag/config.json` 时运行，因此主 CLI 需要先识别 init 子命令，再进入现有项目根解析。

```powershell
python -X utf8 .\scripts\plot_state.py init start `
  --workspace-root PATH `
  --project-root PATH `
  --mode auto|new|ingest|hybrid `
  --target-profile plot_ready|world_bible|normalize_only|continuity_ready `
  --seed TEXT `
  --seed-file FILE `
  --source PATH `
  --interaction-profile minimal|balanced|deep `
  --idempotency-key KEY

python -X utf8 .\scripts\plot_state.py init advance `
  --session-id ID `
  --expected-session-revision N `
  --idempotency-key KEY

python -X utf8 .\scripts\plot_state.py init answer `
  --session-id ID `
  --answers-file FILE|- `
  --expected-session-revision N `
  --idempotency-key KEY

python -X utf8 .\scripts\plot_state.py init inspect `
  --session-id ID `
  --view summary|sources|conflicts|gaps|questions|normalized|diff|proposal

python -X utf8 .\scripts\plot_state.py init dry-run `
  --workspace-root PATH `
  --project-root PATH `
  --mode auto|new|ingest|hybrid `
  --target-profile plot_ready|world_bible|normalize_only|continuity_ready `
  --seed-file FILE `
  --source PATH

python -X utf8 .\scripts\plot_state.py init propose `
  --session-id ID `
  --expected-session-revision N `
  --idempotency-key KEY

python -X utf8 .\scripts\plot_state.py init apply `
  --workspace-root PATH `
  --proposal-id ID `
  --approval-id ID `
  --expected-canon-revision N `
  --idempotency-key KEY

python -X utf8 .\scripts\plot_state.py init verify `
  --project-root PATH `
  --commit-id ID

python -X utf8 .\scripts\plot_state.py init list --workspace-root PATH
python -X utf8 .\scripts\plot_state.py init cancel `
  --workspace-root PATH `
  --session-id ID `
  --expected-session-revision N `
  --idempotency-key KEY
```

`init apply` 已在 v1.0.0 注册；历史 v0.4.5 只实现到冻结 proposal。

`init inspect --view summary` 承担 status 查询；中断后对同一 session 执行 `init advance` 即从最后检查点恢复。

`init dry-run` 默认只输出 stdout；只有显式附加 `--output FILE` 时才保存报告。它不创建 session，需中断恢复的工作应先使用 `init start`。

`init start / dry-run` 可指向尚无 `.plot-rag/config.json` 的目标目录。`init apply` 若未显式传 `--project-root`，会从冻结 proposal 的 `target_project_real_path` 解析目标。

非交互 `init apply` 必须传已有 `--approval-id`；省略时只有真实 TTY 可以连续两次输入完整且完全相同的 proposal ID 来签发短时 grant。CLI 没有 `--yes`。

命令能力矩阵：

| 命令 | session 写入 | canon 写入 | CAS | 幂等键 | grant |
| --- | --- | --- | --- | --- | --- |
| `init start` | 是 | 否 | 无；创建时记录 canon guard | 必需 | 否 |
| `init advance/answer/cancel` | 是 | 否 | `expected_session_revision` | 必需 | 否 |
| `init propose` | 是；冻结 proposal | 否 | `expected_session_revision` | 必需 | 否 |
| `init inspect/list` | 否 | 否 | 无 | 无 | 否 |
| `init dry-run` | 否 | 否 | 无 | 无 | 否 |
| `init apply` | 更新完成状态 | 是 | `expected_canon_revision`；proposal 已冻结 | 必需 | 必需 |
| `init verify` | 否 | 否 | 无 | 无 | 否 |

## 17. MCP

已实现工具：

- `start_story_initialization`
- `dry_run_story_initialization`
- `advance_story_initialization`
- `answer_story_initialization`
- `inspect_story_initialization`
- `build_story_initialization_proposal`
- `apply_story_initialization`
- `verify_story_initialization`
- `list_story_initializations`
- `cancel_story_initialization`

MCP 参数遵循与 CLI 相同的能力矩阵：

- `start_story_initialization` 只要求 `idempotency_key`，因为尚无 session revision；
- `advance`、`answer`、`cancel` 和 `build proposal` 要求 `idempotency_key + expected_session_revision`；
- `inspect`、`list`、`dry-run` 和 `verify` 是只读操作，不要求 CAS；
- v0.5+ 的 `apply` 要求 `idempotency_key + expected_canon_revision + approval_id`，不再接受可变 session 内容。

`apply_story_initialization` 只接受：

- `proposal_id`
- `approval_id`
- `expected_canon_revision`
- `idempotency_key`

stage、路径、文件哈希、授权操作、授权路径和 acceptance source 全部从 grant 与冻结 proposal 派生。

`apply_story_initialization` 已在 v1.0.0 注册；MCP 只消费已有 grant，不提供 grant issuer。

## 18. 硅基流动职责

初始化可复用插件当前硅基流动 Chat 模型：

- Embedding：`BAAI/bge-m3`
- Rerank：`BAAI/bge-reranker-v2-m3`
- 歧义复核：`Qwen/Qwen3-30B-A3B-Instruct-2507`

初始化远端复核默认关闭，只有宿主显式设置下列开关才会启用：

```powershell
$env:PLOT_RAG_INIT_REMOTE_ENABLED = "true"
```

实际调用顺序与权限边界：

1. 本地确定性 inventory、来源分类和 claim 抽取始终先运行。
2. 只有本地分类置信度不足，或本地没有抽出 claim 时，才调用远端歧义复核。
3. 远端来源分类强制降为 `T4 / review / brainstorm`。
4. 远端 claim 强制为 `origin=remote_ambiguity_proposal`、`field_status=model_proposed`、`decision_status=proposed`、`scope=null`。
5. 远端输出只进入 source diagnostics、proposal 和 bundle provenance，不直接补全题材合同、世界规则或剧情发动机，也不拥有 current/timeless/accepted 晋升权。
6. bundle 顶层动态记录 `remote_model_used`、extractor 版本、模型名、cache 命中和响应哈希；纯本地运行保持 `local-deterministic-v1` 与稳定 bundle hash。

本地确定性代码负责：

- 路径和真实目录边界；
- 文件哈希；
- ID、revision、CAS 和幂等；
- schema 和引用校验；
- scope、branch 和 lifecycle 约束；
- 校验、消费并审计可信宿主签发的 approval grant；
- 正典写入和投影；
- secret scanning；
- 恢复和重放。

可信宿主或交互式本地确认通道负责签发 approval grant；初始化服务、CLI、MCP 和远端模型均不具备签发能力。

远端降级规则：

- 超时、限流、错误 JSON 或空响应保留本地确定性结果，并把远端复核标为 failed/degraded；
- 已完成的 inventory、hash 和本地校验不重做；
- 响应按模型、prompt、schema、source hash 缓存；
- one-shot `init dry-run` 只使用进程内临时缓存，不创建持久 cache、index 或 session；
- 重试从当前检查点继续；
- 远端失败不得转化为“来源中不存在该事实”。
- 共享 `SILICONFLOW_API_KEY` 只发往 `api.siliconflow.cn`；非 loopback 必须 HTTPS；HTTP redirect 一律阻断。

## 19. 幂等、恢复与局部失效

稳定 ID：

```text
source_id
= hash(real_path + raw_content_hash)

claim_id
= hash(source_id + evidence_span + normalized_claim)

proposal_id
= hash(canonicalized_proposal_json)

grant_authorization_payload
= canonicalize({
    proposal_id, package_hash, artifact_stage, branch_id,
    chapter_no, artifact_revision, target_project_real_path,
    source_manifest_hash, target_old_new_hashes,
    authorized_operations, authorized_paths,
    expected_canon_revision, issuer, channel, expires_at
  })

approval_binding_hash
= hash(grant_authorization_payload)

accepted_request_hash
= hash(proposal_id + approval_id + approval_binding_hash
       + expected_canon_revision)

commit_id
= hash(proposal_id + approval_binding_hash + previous_canon_revision)
```

恢复规则：

- 每个阶段结束追加 session journal；
- 每次转换使用 `expected_session_revision` 做 CAS；
- 相同 idempotency key 与相同请求哈希返回原结果；
- 相同 key 携带不同请求体返回 `IDEMPOTENCY_CONFLICT`；
- 上游答案变化只失效依赖节点；
- 单个 source 变化只失效依赖它的 extract、conflict、normalize 和 proposal；
- source snapshot 不变时重复 propose 返回相同 proposal；
- 冻结 proposal 后任何答案、源或 canon 变化都会进入 stale/superseded；
- apply journal 覆盖 staging、文件激活、DB commit、projection 和 receipt 检查点；
- projection 失败后从 immutable commit 重建；
- 修正通过 supersede/retract，不手工修改数据库。

## 20. 充分性门禁

`plot_ready` 必须同时通过：

### 20.1 题材

- 主类型发动机；
- 读者承诺；
- 持续回报；
- 差异化变量；
- 调性与边界。

### 20.2 世界

- 规则、稀缺资源、权力分配和当前压力形成闭合因果链；
- 时空、资源、制度、信息和日常生活足以支撑第一卷；
- 世界能在没有主角时运行；
- 主角最自然的方案被世界真实阻断。

### 20.3 人物

- 主角和主动对手都有目标、资源、认知、位置和行动策略；
- 关键关系和第三方利益明确；
- 人物不能使用未获得的信息或不存在的资源。

### 20.4 剧情

- 第一条事件链可启动；
- 成败可判断；
- 失败代价明确；
- 阶段结果会改变状态；
- 第一卷有不可逆变化和后续问题。

### 20.5 连载

- 回报、悬念和兑现窗口可追踪；
- 升级不只等于战力增加；
- 卷级循环可以持续产生新压力；
- 重复度边界明确。

## 21. 世界压力测试

### 21.1 普通一天测试

分别模拟贫困者、普通劳动者和精英二十四小时，检查食物、住房、工作、交通、支付、通信、制度接触和世界差异是否闭合。

### 21.2 三十天无主角测试

移除主角，运行主要组织和压力三十天，检查行动者是否继续行动、资源和关系是否变化、事件是否自然产生。

### 21.3 断供测试

切断一条关键资源或交通线，检查库存、涨价、配给、走私、获利者、受损者和合法性变化。

### 21.4 最优利用测试

让聪明行动者最大化利用某项技术或超凡能力，检查无限财富、无限战力和社会早已普及却未解释的漏洞。

### 21.5 权力真空测试

移除统治者或核心组织，检查法理、武力、财政、信息、继承和争夺过程。

### 21.6 信息泄漏测试

把秘密泄漏给不同阶层，检查可信度、传播渠道、速度、失真、压制、利用和伪造。

### 21.7 跨阶层视角测试

从受益者、执行者、规避者和受害者四个角度检查同一制度。

### 21.8 时空与守恒测试

检查旅时、通信延迟、人物排期、道具唯一性、资源数量和事件先后。

### 21.9 历史反事实测试

删除一个重大历史事件，当前至少三个制度、边界、关系或利益必须随之变化，否则该历史只是装饰。

### 21.10 剧情繁殖力测试

在不新增世界规则的情况下，从当前压力导出：

- 三个即时场景冲突；
- 两条事件链；
- 一个卷级危机；
- 两种主动对手反应；
- 一个解决后仍会留下的后果。

## 22. 测试矩阵

### 22.1 路由与 hook

- 首次初始化请求默认先进入 Grill，每轮只问一个意图问题并提供一个推荐答案；
- 明确初始化合同走零问快通道；skip 在同一轮 handoff，cancel 不创建初始化 session；
- active Grill 中的短续词只重复当前问题，inspect 不推进；
- 只有 handoff 成功并进入 `COMPLETED` 的合同可被后续同类 continuation 复用；
- active 初始化 session 优先于 active Grill 和新初始化请求；
- configless 初始化可以先 Grill 再启动，`grill.enabled=false` 保留直接启动路径；
- Grill 问答、inspect 和 cancel 的 Stop 不抽取剧情 proposal；
- `.plot-rag/grill.sqlite3` 不进入 inventory、authority、bundle、accepted commit 或 replay；
- 空目录和一句话 seed 进入 `new`；
- 有正文、设定或大纲进入 `ingest`；
- 新想法加旧资料进入 `hybrid`；
- 显式 mode 覆盖 auto；
- `auto` 只负责路由，session 最终只保存 `new|ingest|hybrid`；
- 非法 mode/profile 组合返回 `PROFILE_MODE_MISMATCH`，`hybrid + normalize_only` 确定性归一为 ingest；
- 活跃 init session 中的“继续”推进初始化；
- 初始化设计、状态查询和回答不创建剧情 receipt；
- Stop hook 不把初始化 proposal 抽成故事事件。

### 22.2 从零入口

- 完整长段输入：零内容追问；
- 只有题材：最多三个决策包；
- “你来定”：保留 delegated choice 和 origin；
- 无超凡体系：不出现境界链问题；
- 单城故事：不强制大陆地图；
- 修改题材答案：只失效依赖的世界与剧情节点。

### 22.3 已有内容入口

- inventory 前后源文件 SHA-256 一致；
- UTF-8、UTF-8 BOM、GBK、JSON、Markdown、重复文件、空文件和大文件；
- 二进制文件和外部 junction 正确排除；
- 路径命名不会直接授予 T1；
- TODO、灵感和外部参考保持低 tier；
- exact evidence、行号和 source hash 可回溯；
- 昵称合并、同名异人拆分；
- 时间演化与语义矛盾区分；
- outline/current 隔离；
- rich corpus 无核心冲突时零内容追问。

### 22.4 proposal 安全

在 start、advance、answer、inspect、dry-run 和 propose 后：

- canon revision 原值；
- accepted event 数量原值；
- current projection hash 原值；
- authority config 字节原值；
- source bytes 原值；
- init session 不进入 authority index；
- proposal 中的现在时表达不进入 current；
- 源或答案变化使冻结 proposal stale/superseded。

### 22.5 approval 与 apply（v0.5+）

- 缺失、过期、伪造和 hash 不匹配 grant；
- grant 绑定错误 target root；
- target old hash 变化；
- source manifest 漂移；
- canon CAS 冲突；
- 同一 apply 网络重试返回同一 commit；
- 已消费 grant 携带不同请求体失败；
- 每个 apply 检查点崩溃后可恢复；
- accepted manifest 激活前 RAG 保持旧正典；
- 激活后只读取绑定哈希版本。

### 22.6 输出与重放

- 所有 JSON 通过 schema；
- entity、source、claim、proposal 和 commit ID 稳定；
- 一个 schema path 只有一个 owner；
- unknown、deferred 和 conflicted round-trip 不变；
- import → normalize → 再 import 产生 zero diff；
- initialization commit 重放得到相同规范化哈希；
- completion receipt 与文件、事件和投影一致。

### 22.7 规模与远端降级

- 500 章增量 inventory/extract；
- 未变化文件跳过解析、Embedding 和抽取；
- 硅基流动超时、限流、空响应和错误 JSON；
- 远端恢复后从原阶段继续；
- 日志、proposal、commit 和错误信息 secret scan 为零；
- Windows 中文路径、大小写归一和长路径。

量化门禁：

| 指标 | 目标 |
| --- | --- |
| Grill 每轮问题数 | `<= 1` |
| Grill 推荐答案数 | `<= 1` |
| 可检索项目事实误追问 | `0` |
| 明确初始化请求多余 Grill 轮次 | `0` |
| 非 `COMPLETED` 合同复用 | `0` |
| Grill 问答轮 init session 创建 | `0` |
| Grill 问答轮剧情 receipt/proposal | `0/0` |
| `preapproval_canon_delta` | `0` |
| `source_bytes_preapproval_mutation` | `0` |
| `init_workflow_plot_receipt_count` | `0` |
| `repeat_question_count` | `0` |
| 完整 seed 内容问题包 | `0` |
| 稀疏 new 内容决策包 | `<= 3` |
| rich ingest 无冲突内容问题包 | `0` |
| 幂等 proposal/commit ID 一致率 | `100%` |
| normalize round-trip zero diff | `100%` |
| initialization replay 稳定哈希 | `100%` |

## 23. 分阶段实现

### v0.4.5：双路径三模式初始化协议

历史阶段完成：

- [x] init session、journal、canon guard 和 session revision；
- [x] `new`、`ingest`、`hybrid` 路由；
- [x] 题材合同、世界因果核、人物锚点、剧情发动机和连载合同 schema；
- [x] 自适应最少提问；
- [x] inventory、classify、extract、conflict、gap 和 normalize；
- [x] InitializationBundle v1；
- [x] start、advance、answer、inspect、dry-run、propose、list 和 cancel；
- [x] 初始化目录与剧情 hook、authority index 的硬隔离；
- [x] 中断恢复、局部失效、幂等和 stale 检测。

该历史阶段不允许初始化内容进入正典；v1.0.0 继续完成后续 grant-gated apply。

### v0.5.0：初始化验收与连续性模型

已完成：

- [x] approval grant 消费；
- [x] `init apply`；
- [x] accepted source manifest；
- [x] 类型化 initialization commit；
- [x] correction、supersession 和 retraction；
- [x] 标准文件 staging、激活和恢复；
- [x] state、index、snapshot、summary 和 vector 确定性投影；
- [x] apply journal、恢复和 completion receipt。

### v0.6.0：大型作品增量整理

- [x] 大型语料增量 inventory/extract；
- [x] source、Embedding、Rerank 和 response cache（远端响应键绑定 model、prompt、schema 与 source hash，支持 TTL、LRU、维度失效和递归去敏；one-shot dry-run 仅使用进程内缓存）；
- [x] 500 章规模门禁；
- [x] normalize round-trip（标准 envelope 支持 export → ingest → normalize → re-import 的 zero diff 与稳定语义哈希，并保留 unknown、deferred、conflicted、ownership、conflict 和 provenance）；
- [x] projection retry 和 replay；
- [x] 只重算受变更来源影响的依赖子图。

### v1.3.0：力量体系初始化

- [x] InitializationBundle v1/v2 自动协商；
- [x] `plot-rag-power/v1` 统一力量本体；
- [x] 12 个声明式题材 Adapter；
- [x] 本地与远端 `ability.*` claim 字段保真；
- [x] 体系、成长轨、阶段、晋升边、能力、资源、状态定义、克制、桥接和资源转换标准化；
- [x] 角色 progression、ability、resource、status、binding、qualification bootstrap；
- [x] 力量来源、资源循环、能力合同、晋升失败和社会后果充分性门禁；
- [x] mundane 零境界链；
- [x] 定义 proposal 与 initialization runtime proposal 分权；
- [x] 两个独立 grant、两个 canon revision CAS 与 completion receipt v2；
- [x] normalized v2 round-trip 与稳定 bundle hash。

### v1.4.0：Grill 初始化接入

- [x] 首次初始化请求接入 `plot-rag-intent/v1` 八字段合同；
- [x] 默认单问、推荐答案、项目先探测与零问快通道；
- [x] skip、cancel、repeat、inspect 和问题上限语义；
- [x] configless 初始化先 Grill 后同轮 handoff；
- [x] active 初始化 session 保持最高优先级；
- [x] Grill 问答 Stop 与剧情 proposal 抽取隔离；
- [x] 只复用 `COMPLETED` 合同；
- [x] `.plot-rag/grill.sqlite3` 非正典隔离、TTL 和 turn 幂等；
- [x] `grill.enabled=false` 兼容与 v1.3.0 迁移说明。

## 24. 完成定义

初始化框架已满足以下条件：

- [x] 用户可以从一句话开始，按“题材 → 世界 → 剧情”建立 plot-ready 作品。
- [x] 用户可以对已有内容运行只读 inventory 和 dry-run，并看到来源、冲突、缺口、标准化结果和逐文件 diff。
- [x] 两条来源路径生成同一种 InitializationBundle。
- [x] 新初始化请求先锁定任务意图；明确输入可零问 handoff，模糊输入每轮只问一个问题。
- [x] 初始化中断后可以从检查点继续，不重复提问。
- [x] proposal 前后正典、authority config 和源文件保持零变化。
- [x] approval grant 之前没有任何路径能把初始化结果晋升为正典。
- [x] accepted commit 可以确定性重放标准文件和运行投影。
- [x] 活跃初始化会话中的“继续”不会触发剧情 receipt。
- [x] 活跃 Grill 中的“继续”只重复当前问题，Grill 问答不创建初始化 session 或剧情 proposal。
- [x] Grill 合同和数据库不进入初始化 bundle、正典或 replay，且可通过 `grill.enabled=false` 独立回滚。
- [x] 世界框架包含 MVW、因果、日常、压力和反事实测试。
- [x] 初始化完成后，剧情推演能够直接查询角色状态、关系、位置、道具、时间、认知、世界规则和活跃压力。
- [x] 结构化力量作品可直接查询体系、阶段、能力、资源、冷却、状态、来源绑定、资格和已知克制。

### 24.1 实现与验证证据

发布候选初始化验证结果：

- 全量自动化的当前数量以 v1.4.1 严格测试和最新 CI run 为准；历史 293-test 快照不再作为当前发布证明。
- 合成 fixture 只读 ingest dry-run 覆盖来源发现、显式冲突、hard gap、`database_touched=false` 与 `persisted=false`，并验证输入树前后逐文件一致。
- 临时 `new / ingest / hybrid` 三模式均完成 apply、verify 并达到 `BOOTSTRAP_READY`；每种模式继续接受 3 章后仍为 ready。
- initialization replay 哈希稳定；accepted source manifest 的路径/哈希校验 0 failure；longform 状态 ready。
- 远端 response cache、local-first 歧义复核 provenance 与 normalize round-trip fixture 已覆盖；硅基流动 Embedding、Rerank、Chat 三端真实请求均返回 HTTP `200`。

主要自动化证据：

- `tests/test_plot_init.py`：模式路由、目标档位、dry-run 零写入、来源编码、claim/conflict/gap、checkpoint/resume、CAS、幂等、stale、hook 仲裁和 schema/template。
- `tests/test_plot_init_cache_roundtrip.py`：进程内/SQLite 远端缓存、TTL/LRU/维度失效/去敏，以及 export、re-import、zero diff 和稳定语义哈希。
- `tests/test_plot_init_remote.py`：显式 opt-in、本地优先、低置信分类、零 claim、T4/proposal 强制、provenance、host/HTTPS/redirect/secret 门禁。
- `tests/test_plot_init_authority_gate.py`：draft、outline、T4、review、未决冲突与模型默认值的正典隔离。
- `tests/test_plot_init_world_pressure.py`：固定十项世界压力测试的 required/observed evidence、score、diagnostic 与 pass/degraded/fail。
- `tests/test_inventory_conservation.py`：acquire/set/transfer/consume/lose 守恒、余额不足回滚和 replay。
- `tests/test_release_hardening.py`：hash 绑定、来源分类、配置合并、secret scrubber 与 materialization 安全。
- `tests/test_continuity_lifecycle.py`：初始化 package adapter、grant、类型化 apply、accepted source manifest、materialization saga、来源漂移和 replay。
- `tests/test_v1_runtime.py`：从冻结 proposal 到 grant、apply、materialize、verify、`COMPLETED` 和 `BOOTSTRAP_READY` 的完整链路。
- `tests/test_cli.py`：无现有 config 的 start/dry-run、命令映射、非 TTY fail-closed、真实 TTY 双重 proposal ID 确认、已有 grant 消费和 init apply 目标解析。
- `tests/test_hook.py`：无现有 config 的初始化启动、active session 优先、Stop 抑制剧情抽取和完成会话释放 hook。
- `tests/test_mcp.py`：无需既有 config 的 start/dry-run、apply 消费宿主 grant 和只读注解。
- `benchmarks/fixtures/chapters_500.v1.jsonl`：大型作品增量索引、上下文预算和未变化来源零重解析。
