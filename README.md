# plot-rag-gate

面向长篇网文的剧情连续性、作品初始化与创作方法 RAG 门禁。

`v1.6.4` 修正 Codex 运行时集成：Hook 清单只声明宿主支持的
`SessionStart / UserPromptSubmit / Stop`，未决 extraction 屏障在同一个 `Stop`
边界持久化，不再依赖宿主没有提供的 `SessionEnd` 事件。内部 `--session-end`
入口继续保留，供兼容调用和直接诊断使用。v1.6.3 的通用发行清理继续有效：
生产载荷不内置任何单一作品的名称、角色、设定、绝对路径、验收快照或专属
Advantage seed。
独立 PowerSpec 导入入口继续接受完整
`plot-rag-power/v1` 可直接执行 validate → preview → immutable
`power_spec_change` proposal → `accept_power_spec` grant → canon CAS → replay，
不再必须绕经初始化 v2；validate/preview 保持零项目写入，propose 只冻结 proposal，
MCP 仍无 grant 签发权。v1.6.1 的 accepted source-manifest 生命周期、v1.6.0 的
`plot-rag-advantage/v1`、continuity schema v7、16 类 profile、三套独立
projection hash 与保守兼容开关继续保持原语义。

v1.5 的精确性能路径继续保留：Prepare v2 使用单快照与 revision/projection CAS、accepted 精确状态短路、provider-aware Embedding、有界并行 Rerank、SingleFlight、精确缓存、连接复用和阶段遥测；SiliconFlow `api.siliconflow.cn + BAAI/bge-m3` 强制 singleton-exact，并按 `min(remote_total_concurrency, 4)` 有界并发、按输入顺序归并。

## 设计文档

- [WEBNOVEL_INITIALIZATION_FRAMEWORK.md](WEBNOVEL_INITIALIZATION_FRAMEWORK.md)：`new / ingest / hybrid` 初始化协议、真实虚拟世界框架、标准文件投影与 apply 流程。
- [POWER_SYSTEM_ADAPTATION_PLAN.md](POWER_SYSTEM_ADAPTATION_PLAN.md)：修仙、魔法、技能、游戏升级及混合体系的统一力量内核、题材适配器、迁移与测试路线。
- [POWER_SYSTEM_MIGRATION.md](POWER_SYSTEM_MIGRATION.md)：continuity schema v5、旧 ability 投影、初始化 v2、shadow/strict 切换与回滚说明。
- [docs/GRILL_INTENT_GATE.md](docs/GRILL_INTENT_GATE.md)：`plot-rag-intent/v1` 合同、单问状态机、Hook 顺序、初始化接入、迁移与回滚。
- [docs/ITEM_FUNCTION_MODEL_BACKLOG.md](docs/ITEM_FUNCTION_MODEL_BACKLOG.md)：通用 Item definition、instance、function、runtime 与迁移边界。
- [V1_5_MIGRATION.md](V1_5_MIGRATION.md)：continuity v5→v6 在线备份、双 projection hash、legacy item、初始化 sidecar、分阶段启用与回滚。
- [V1_6_MIGRATION.md](V1_6_MIGRATION.md)：continuity v6→v7、Advantage sidecar、独立 PowerSpec 导入、三 projection hash、CLI/MCP、可读投影与回滚。

## 核心能力

- **严格正典生命周期**：proposal、grant、CAS、accept、reject、retract、correction、supersession 与确定性 replay。
- **Grill 意图门禁**：默认启用；先检查项目可检索面，只追问用户意图，每轮一个问题并提供推荐答案，锁定八字段合同后再执行。
- **事件体验门禁**：每个 EventSeed 在推演和设计前先锁定目标情绪、压力曲线、释放方式、余味与禁止误读；accepted outline 派生结果绑定 commit/artifact/revision/content hash，结构性歧义最多追加一个单问。
- **类型化连续性**：角色、别名、关系、位置、移动、库存转移、故事时间、能力、代价、知识、承诺、债务与 open loop。
- **通用物品功能模型**：Definition、Instance、Stack、Function、Custody、Runtime、UseHistory 与 Observation 分层，所有权和物理保管分别记录。
- **特殊物品与金手指**：Advantage Definition、Anchor、Module、Runtime、Ledger、Knowledge、Contract、Narrative Contract、Progression 与 Exposure 独立建模。
- **统一力量体系**：定义、持有、可用和使用分离；境界/等级、技能、资源、冷却、状态、来源绑定、资格、观察和跨体系规则分别建模。
- **双路径三模式初始化**：既可按“题材 → 世界 → 剧情”创建新作，也可只读整理已有正文、设定、大纲和笔记。
- **长篇召回**：持久化 FTS5/BM25、SHA-256 增量索引、一至五条原子连续性需求、mandatory context contract。
- **精确性不降级的提速**：单 accepted read snapshot、provider-aware Embedding（SiliconFlow BGE-M3 为 singleton-exact；其他已证明 batch-independent 的模型才批量）、单条故障隔离、有界并行 Rerank、按 need 稳定归并、revision-aware cache 与下一剧情轮抽取屏障。
- **Rerank 精确缓存**：进程级 4096 项 LRU；相同 exact request 通过 SingleFlight 合并，缓存只接受已验证的 `(index, score)`，失败、畸形、NaN/Inf、重复或越界 index 不写入健康缓存，失败 flight 立即清理并允许健康重试。
- **三层记忆**：working、episodic、semantic memory，以及真正参与任务召回的章、事件弧、卷三级摘要。
- **网文方法包**：按题材、产物阶段、任务和连续性风险召回方法；只从 accepted 内容学习项目模式。
- **可恢复派生投影**：snapshot、index、summary、memory、vector 分别记录运行日志，可独立 retry/replay。
- **硅基流动默认配置**：Embedding、Rerank 和结构化抽取共用一个环境变量密钥。

## 力量体系引擎

力量体系采用“统一内核 → 题材 Adapter → 项目原生词汇”三层结构：

```text
PowerSystemSpec
  → ProgressionTrack / RankNode / RankEdge
  → AbilityDefinition / ResourceDefinition
  → CounterRule / BridgeRule
  → Actor progression / ability ownership / runtime
  → resource / status / binding / qualification / observation
  → typed event / accepted projection / replay
```

内置 12 个声明式 profile：

- `cultivation`
- `magic`
- `skill_tree`
- `game`
- `martial`
- `superpower`
- `bloodline`
- `technology`
- `contract_summoning`
- `system_assist`
- `hybrid`
- `mundane`

核心规则：

- 定义、持有、当前可用性和一次具体使用分别保存；
- 角色可并行拥有多条成长轨，不自动折叠成综合战力；
- 战力比较返回条件、优势面、缺口和证据，不输出默认单一数值胜者；
- 比较结果是 `derivation=query_time`、`persisted=false` 的派生
  `ComparisonClaim`，不写入正典；
- 晋升必须匹配 accepted `RankEdge`，并通过前置、资源、资格和故事时间校验；
- 冷却、恢复和到期使用结构化 `StoryCoordinate`，不读取真实墙钟代替故事时间；
- 物品与抽象资源分离；跨资源、跨体系换算依赖 accepted `ConversionRule` 或 `BridgeRule`；
- 客观机制、人物认知、公开叙事、读者已知和作者计划继续隔离；
- 世界级定义变化使用独立 `power_spec_change` proposal 和 `accept_power_spec` grant；
- 普通章节只提议角色运行态与观察事件，不静默改写整套世界规则。

初始化完成后补录或修订整套力量定义时，使用独立入口：

```text
plot-rag-power/v1 aggregate
→ validate（纯编译）
→ preview（绑定 accepted canon revision，零写入）
→ propose（原子注册稳定实体并冻结 proposal）
→ trusted host grant(accept_power_spec)
→ proposal accept + canon revision CAS
→ accepted commit
→ replay / power query / doctor
```

独立导入只接收体系、成长轨、阶段、晋升边、能力、资源、状态、资格、克制、桥接
和转换等**世界级定义**。角色当前境界、能力持有、资源余量、状态、资格和观察仍走
普通 `story_delta`，不会借导入入口静默写成当前剧情事实。

## 通用物品功能模型

schema v6 在 legacy `inventory_state` 之外增加独立、可重放的物品投影：

```text
ItemDefinition
  → ItemFunctionDefinition / ItemFunctionBinding
  → ItemInstance / ItemStack
  → ownership + custody + containment
  → runtime + function runtime
  → use history + observation
  → item_projection_hash
```

核心规则：

- accepted immutable event 是唯一权威；物品表、可读快照和 `item_projection_hash` 都可从事件重放；
- 物品“属于谁”与“现在由谁或哪个容器保管、位于何处”分开记录；
- 唯一物品使用 instance，批量同质物使用 stack；数量、耐久、充能、冷却、封印和功能解锁分别校验；
- 功能必须来自 accepted 定义或显式绑定，不根据名称、旧 `attributes_json` 或题材常识猜测；
- 使用事件必须同时满足保管/所有权、功能解锁、资源、耐久、冷却、资格、位置、力量绑定和知识平面要求；
- 战斗、解谜、逃生、生产、治疗、交易、权限和证据任务把相关物品定义、功能、custody 与 runtime 列入 mandatory context；
- 新物品事件使用 `plot-rag-delta/v4`；旧 `plot-rag-delta/v3` 与 `inventory` 继续兼容，旧连续性 projection hash 的定义不变。
- `items.strict_runtime_validation=false` 时 v4 只做 reducer dry-run 与 warning diagnostics，authority accept 在消费 grant 前以 `ITEM_STRICT_RUNTIME_DISABLED` 阻断；legacy v3 inventory 不受影响。
- `power_binding_bridge=false` 阻断新 ability bridge；启用时复用既有 AbilityDefinition，item 层不复制 effect、cost 或 cooldown。
- `readable_projection=true` 时，accept/retract/replay 原子刷新 `.plot-rag/物品/物品索引.md`、定义卡与实例卡；失败只降级可读投影，不改变 accepted canon。

## 特殊物品与金手指

schema v7 增加独立的 Advantage projection：

```text
AdvantageDefinition
  → AdvantageAnchor
  → AdvantageModuleDefinition
  → runtime slots + branch runtime
  → reward/cost/conversion ledger
  → five-plane knowledge
  → in-world contract + narrative contract
  → progression + exposure
  → advantage_projection_hash
```

内置 16 类 profile：

`inheritance`、`resource_transformer`、`growth_relic`、`pocket_domain`、
`companion_mentor`、`system_panel`、`task_reward`、`reward_market`、
`appraisal_copy`、`simulator_branch`、`foreknowledge`、`time_causality`、
`contract_summon`、`bloodline_constitution`、`social_currency`、
`sign_in_lottery`。

核心规则：

- 金手指不等于物品：物理载体引用 Item stable ID；虚拟系统、知识、时间、血脉和契约可使用非物理 anchor；
- 每个模块显式保存 trigger、preconditions、targets、costs、effects、side effects、failure modes 与 counterplay；
- reward、cost、loss、conversion 和 provenance 进入因果账本，资源与数量由本地 reducer 守恒；
- objective、actor belief、public narrative、reader disclosed、author plan 五层知识隔离；
- `canon|planned|rumor|misread` 与 reveal stage 分离，未来能力和误解不自动晋升当前运行态；
- 每次相关剧情设计先锁定读者体验，再注入同一 accepted snapshot 下的 Advantage、Item、Power、关系、位置和故事时间；
- accepted event 是唯一权威；`.plot-rag/金手指/` 与 `.plot-rag/advantages.v1.json` 都有独立来源/hash 合同；
- continuity、item、Advantage 三套 projection hash 互不混合，可分别 replay 和回滚核验。

## Grill 意图门禁

Grill 是剧情推演与作品初始化之前的非正典控制层。它只记录“这次任务为什么做、交付什么、做到哪里、哪些不能动”，不记录作品事实，也不直接读取或修改连续性正典。

`plot-rag-intent/v1` 固定包含八个字段：

1. `problem_to_solve`
2. `expected_deliverable`
3. `reader_experience`
4. `protagonist_drive_conflict`
5. `scope_endpoint`
6. `success_criteria`
7. `hard_constraints`
8. `model_autonomy`

默认行为：

- `grill.enabled=true`，每轮最多一个问题，并附一个推荐答案与简短理由；
- 先探测项目配置、authority 和连续性存储；角色位置、道具、力量、关系和故事时间等项目事实留给后续 RAG，不拿来盘问用户；
- 输入已明确问题、交付物、范围，且具备足够的体验、冲突、成功标准或约束时，走零问快通道；
- 明确说“跳过 Grill”“按现有要求直接执行”等会用可追溯默认值补齐合同并立即交接；
- 明确说“取消本轮 Grill”会结束本轮，不执行原创作任务；
- 活跃 Grill 中的“继续”“开始吧”“下一步”只重复当前问题；“为什么问这个”“还剩几题”只查看状态，不消费答案；
- 剧情续写只复用同项目、同宿主会话、同任务族、TTL 内且状态为 `COMPLETED` 的合同；`AWAITING_ANSWER`、`EXECUTING` 或失败交接不会被当作已完成共识；
- `.plot-rag/grill.sqlite3` 是项目本地、非正典、可丢弃的控制状态，不进入 authority、长篇索引、proposal、accepted commit 或 replay；
- 设置 `grill.enabled=false` 后恢复 v1.3.0 的直接初始化/剧情 prepare 路径。

## 事件级读者体验门禁

`EventExperienceContract` 不代替 Intent Contract。Intent Contract 约束整轮任务，体验合同约束一个具体叙事事件：

```text
Intent Contract
  → EventSeed / EventExperienceArc
  → EventExperienceContract
  → receipt
  → proposal
  → grant
  → accepted commit
  → ExperienceReview
```

- 每个要被设计或推演的事件必须先有稳定 `EventSeed`，并一对一绑定 locked experience contract；
- config v3 默认 `event_experience.enabled=true` 且 `required_before_event_design=true`；缺少 locked contract、manifest/hash 不一致或 accepted outline 漂移时，在零 receipt、零远端调用处硬阻断；
- 合同记录目标主情绪、次情绪、进入状态、压力曲线、峰值、释放方式、结束余味、预期行为反应与禁止误读；
- 高置信度信息从用户 Intent、accepted 大纲与当前事件链自动派生；accepted outline 必须同时绑定源 commit ID、artifact ID/version/revision 与内容 SHA-256，任一身份或内容漂移都会让旧 manifest 失效；只有结构性歧义才进入独立 `phase=event_experience` 单问；
- 同一事件链最多提出一个新问题；无效回答只原样重复一次，“继续”不等于接受推荐方向；
- 缺少 locked contract 时不创建剧情 receipt，也不发起远端检索或生成；
- receipt、proposal、grant 和 accept 必须携带同一组 event seed / contract ID、revision 与 hash；
- 升级前已有的 accepted 正文、章纲和大纲全部 grandfather：不回填 intended contract，不修改既有 canon、commit 或 replay hash；旧文本最多建立 `grandfathered_observed_only` review；
- 控制层使用独立 binding revision CAS，不推进 canon revision；ExperienceReview 使用独立 review revision，记录、失败或 supersede review 不推进 lifecycle binding，也不改变 proposal、job 或 canon；
- 合同与 review 从 authority、FTS、vector、摘要和三层记忆中硬排除；自动 review 失败只追加脱敏的 `.plot-rag/experience-review-diagnostics.jsonl`。

## 严格剧情流程

```text
UserPromptSubmit
  → 活跃初始化会话优先
  → 活跃 Grill 接管回答 / 重复 / inspect / skip / cancel
  → 新的剧情或初始化创作请求进入 Grill
      → 项目可检索面探测
      → 零问快通道，或每轮一个问题 + 推荐答案
      → 锁定八字段 Intent Contract
      → 生成 EventSeed / EventExperienceArc
      → 自动锁定体验合同，或结构性歧义单问
      → 零远端调用校验 experience manifest
  → 初始化 handoff，或剧情 prepare
  → 固定 canon revision + projection hash
  → 单一 accepted read snapshot
  → 权威来源 SHA-256 增量索引
  → accepted 精确状态与 active/timeless 投影
  → FTS5/BM25 + provider-aware exact Embedding + 有界并行 Rerank
  → working / episodic / semantic memory
  → 章 / 弧 / 卷摘要与网文方法卡
  → receipt + prepared_canon_revision + experience hashes
  → 模型推演

Stop
  → 活跃初始化或 Grill 问答轮：抑制剧情 proposal 抽取
  → 已交接剧情轮：
  → 检查故事产物是否泄漏内部 sentinel、schema、类型名或 identity 字段
  → 命中 `STORY_ARTIFACT_CONTROL_TERM_LEAKAGE` 时保持零 proposal、零 job
  → sync 抽取，或登记绑定完整身份的 durable extraction job
  → 硅基流动 Chat 严格 JSON 抽取；下一剧情轮经过一致性屏障
  → 本地 schema、逐字证据与类型化语义校验
  → immutable proposal，或确定性 no-delta
  → 自动 ExperienceReview；失败只写 diagnostics
  → 刷新同 branch / sequence 的 extraction 与 pending-review 屏障
  → 将 queued / running / failed / pending-review 持久化到
    .plot-rag/session-close-pending.json
  → accepted / no-delta 清除对应记录；损坏文件 fail-closed
  → 不改变 current、timeless 或 accepted event

可信宿主或真实交互式本地确认
  → 签发一次性 approval grant

accept
  → 校验 proposal、grant、stage、branch、revision 与 CAS
  → 原子消费 grant
  → immutable accepted commit
  → append-only typed events
  → current / planned / historical / timeless / branch projections
  → snapshot / index / summary / memory / vector 独立投影

```

第三方模型没有文件、数据库、grant 或正典写权限。模型输出始终只是候选；ID、作用域、所有权、顺序、版本、幂等、CAS 和写入由本地确定性代码控制。

## 产物阶段与正典规则

| 维度 | 规则 |
| --- | --- |
| `brainstorm` | fail-closed 默认阶段；即使 accepted 也不进入权威 current |
| `outline` | accepted 后所有事实一律进入 `planned`；即使候选请求 `timeless` 也不会进入 active/timeless |
| `draft` | 保留在对应 branch 的 provisional facts |
| `bootstrap` | 经初始化审批后可写入类型化初始正典 |
| `final / published` | accepted 后可更新权威 current |
| `timeless` | 进入独立 `timeless_facts`，查询时与有效时点状态合并 |
| `historical` | 保留历史事件，不覆盖当前时间线 |

`UserPromptSubmit`、`Stop`、普通 CLI、MCP 和硅基流动模型都没有 grant 签发权。MCP 只消费已有 `approval_id`；非交互命令不提供跳过确认的自动批准参数。

## 作品初始化

### 模式

```text
new
  题材合同 → 世界因果核 → 人物锚点 → 剧情发动机 → 连载兑现合同

ingest
  只读 inventory → 来源分类 → claim → 实体/别名/时间归一
  → 冲突图 → 缺口矩阵 → 标准化 proposal

hybrid
  先 ingest，再只补真正阻塞下游的缺口
```

支持四个目标档位：

- `plot_ready`
- `world_bible`
- `normalize_only`
- `continuity_ready`

支持 `minimal / balanced / deep` 三种交互档位。初始化引擎实现：

- 最小可运行世界 MVW；
- 九类底层对象、十三个运行模块和五级世界分辨率；
- 十项日常、压力与反事实测试；
- 客观真相、人物认知、公开叙事、读者已知和作者计划五类知识平面；
- checkpoint/resume、session revision CAS、幂等键、source/canon stale 检测；
- host session/turn 强绑定；同一宿主出现多个精确活跃库时阻断仲裁，缺稳定 turn identity 时只读 inspect；
- 初始化存储 schema v2：完整 JSON 以 SHA-256 内容寻址 blob 去重，超过阈值时 zlib 压缩，读取时校验引用、长度、编码和哈希，单 payload 默认上限 64 MiB；
- 旧 inline JSON 保持混读；批量迁移必须显式执行 dry-run/inspect，随后先做 SQLite online backup，再在事务中改写引用，可选清理孤儿 blob 与 VACUUM；
- 零写入 dry-run、不可变 `InitializationBundle v1/v2` proposal 和逐文件 diff；
- 初始化协议自动协商：通用作品继续使用 v1；出现结构化力量 claim、显式 profile 或力量对象时进入 v2；
- v2 额外保存体系、成长轨、阶段、晋升边、能力、资源、克制、桥接和角色力量 bootstrap；
- 力量专项充分性检查覆盖来源、当前阶段、资源循环、能力代价/边界/反制、晋升失败与社会后果；
- approval、类型化 apply、staging materialize、accepted source manifest、verify；
- `COMPLETED` 会话终态与 `BOOTSTRAP_READY` 项目状态。

原始资料始终只读。标准 Markdown 是 accepted initialization commit 的人类可读投影；proposal 冻结前后都不会自动覆盖源文件或污染正典。

初始化会话拥有最高创作工作流优先级：活跃会话中的“继续”和有效回答推进初始化，初始化 inspect/propose/cancel 直接进入对应操作，`Stop` 同时抑制剧情事件抽取。插件维护、分析、审查、测试、仓库和发布等元任务保持静默，不被 active session 消费。首次“初始化一部作品”会先经过 Grill；零问、合同完成或明确 skip 后，在同一轮交给初始化启动。

## 长篇召回与网文方法

### 权威检索

- `authority_sources` 为每个 glob 标记 `role`、`scope_policy`、`ingest_policy` 和优先级。
- `.git/**`、`.plot-rag/**`、`.plot-rag-init/**`、外部 symlink/junction 等路径被硬排除。
- 未变化文件只读取字节并比较 SHA-256；不会重新解析、分块或调用 Embedding。
- FTS5 可用时使用 BM25；不可用时保留持久化本地 fallback。
- prompt 被拆为一至五条原子连续性需求，并按任务分配状态、关系、位置、库存、时间、力量运行态与 open-loop 配额。
- 战斗、突破、训练、装备、系统奖励和契约任务拥有 mandatory power context，定义平面与当前运行态优先于相似正文片段。
- Prepare v2 的 Embedding 路由按 provider/model 语义选择：批量接口只有在 batch 与 singleton 向量已验证等价时启用；SiliconFlow `BAAI/bge-m3` 走 singleton-exact，每条 query 独立失败隔离，并在 `remote_total_concurrency` 预算内有界并发，完成后按原 need 顺序归并。
- Rerank 远端调用按 need 有界并行，**不改变 legacy provider 返回顺序**；候选/lexical 阶段继续使用固定 tie-break，避免 v1/v2 在相同 provider 响应下发生二次排序漂移。
- Rerank exact cache 为进程级 4096-entry LRU。显式 `cache_identity` 可让独立 provider wrapper 共享结果；未声明 identity 时缓存保留 provider 对象并只接受同一对象，避免对象 id 重用造成串值。失败、降级、validator 未通过或畸形结果不缓存。
- 远端超时、限流或空召回只会降级，不会被解释成“事实不存在”。

### 记忆与投影

- working memory：当前任务必须携带的精确状态和活跃剧情债务；
- episodic memory：accepted 章节与事件经历，按 branch、chapter、arc、volume 和任务预算召回；
- semantic memory：稳定世界规则、人物长期信息和跨章规律，按当前任务相关性召回；
- chapter / arc / volume summary：只从 accepted commit 生成，并按 outline / scene / prose / revision 的局部性顺序参与上下文；
- 精确 current/timeless 状态始终排在历史记忆和摘要之前；历史章节不会越过目标章节注入未来信息；
- snapshot / index / summary / memory / vector：各自拥有 projection log，可独立重试和重放；
- 同一 `(projection, commit, normalized input)` 由 SQLite 原子 claim 保证单一 owner；并发调用与 recovery 等待既有运行并复用结果，`force` 刷新不会被旧 output 提前满足；
- `longform recover --run-id` 只接管精确指定且已确认 ownerless / owner 已死亡的陈旧运行；live 或无法验证的 owner 保持 fail-closed，不会并发重跑；
- 只有可再生缓存与投影运行历史执行有界保留，正典事件与 accepted commit 不被压缩删除。

### 网文方法包

`knowledge/webnovel_methods.json` 提供章首承接、章末钩子、悬念兑现窗口、爽点蓄压与释放、升级资源账、能力代价、群像轮转、重复度控制和卷末高潮等方法卡。方法卡按 `genre`、`artifact_stage`、`task` 和 `continuity_risk` 过滤，只组织当前推演，不是项目事实源，也不会把检查表写进正文、章纲或大纲。

## Hook、CLI 与 MCP

### Hooks

- `SessionStart`：只读诊断，不创建状态库。
- `UserPromptSubmit`：依次处理活跃初始化、活跃 Grill、新创作请求 Grill、初始化 handoff 或剧情 prepare。
- `Stop`：活跃初始化和 Grill 问答轮抑制剧情抽取；已交接的 config v3
  剧情轮只保存 proposal；config v1/v2 保持旧版兼容提交行为。抽取或排队完成后，
  同一 Stop 边界会把 queued、running、failed 与 pending-review 屏障脱敏持久化，
  供下一剧情轮继续处理。
- `--session-end` 仍是兼容和直接诊断入口，但不登记为 Codex Hook 事件。

元问题、插件说明、流程设计、插件维护、仓库、测试、发布、否定、暂停、分析、审查和查询不会触发剧情闭环，也不会被活跃初始化当成回答；“给剧情推演增加测试”“优化剧情推演正则”“剧情推演的关键词有哪些”等明显在讨论门禁本身的说法同样保持静默。没有活跃 Grill 时，“继续”“开始吧”等短续词仍继承最近有效任务分类；存在活跃 Grill 时，这些词只重复当前问题，不会被记作答案。显式“剧情推演”“推演下一章”始终优先，“续一章”“再来一章”“把下一章写出来”“把章纲扩成正文”等自然网文创作指令也会进入同一创作仲裁。

### CLI

统一入口：

```powershell
python -X utf8 .\scripts\plot_state.py --version
python -X utf8 .\scripts\plot_state.py --help
```

顶层剧情、查询、诊断、重放和迁移命令：

```powershell
python -X utf8 .\scripts\plot_state.py prepare `
  --project-root "D:\novel" `
  --prompt "设计第十二章章纲" `
  --artifact-stage outline `
  --chapter-no 12

python -X utf8 .\scripts\plot_state.py propose `
  --project-root "D:\novel" `
  --assistant-file ".\chapter-12-outline.md"

python -X utf8 .\scripts\plot_state.py query `
  --project-root "D:\novel" `
  --query "主角当前的位置、伤势和持有道具"

python -X utf8 .\scripts\plot_state.py query-at `
  --project-root "D:\novel" `
  --mention "测试角色甲" `
  --chapter-no 12 `
  --scene-index 3

python -X utf8 .\scripts\plot_state.py craft `
  --project-root "D:\novel" `
  --query "设计章末钩子"

python -X utf8 .\scripts\plot_state.py doctor --project-root "D:\novel"
python -X utf8 .\scripts\plot_state.py replay --project-root "D:\novel"
python -X utf8 .\scripts\plot_state.py migrate --project-root "D:\novel" --component all --dry-run
```

`replay` 会重放 accepted continuity，并用同一次 `query_facts` 的
current + timeless 权威结果同时重建
`.plot-rag/continuity_snapshot.json` 和 legacy-shaped
`.plot-rag/state_snapshot.json`；config v3 不读取可能陈旧的
`current_facts`。关系维度保留原始 field，派生 fact key 同时绑定
scope 与维度，避免 current/timeless 或多维关系互相覆盖。它还会重放
long-form memory、summary 与 project pattern 派生层。

`commit` 是 `propose` 的兼容名；在 config v3 下仍只生成 proposal。`query-at` 只用于 config v3 严格生命周期。

proposal 生命周期：

```powershell
python -X utf8 .\scripts\plot_state.py proposal list `
  --project-root "D:\novel" `
  --canon-status proposed

python -X utf8 .\scripts\plot_state.py proposal inspect `
  --project-root "D:\novel" `
  --proposal-id "proposal-..."

python -X utf8 .\scripts\plot_state.py proposal reject `
  --project-root "D:\novel" `
  --proposal-id "proposal-..." `
  --reason "与正典冲突" `
  --idempotency-key "reject-001"

python -X utf8 .\scripts\plot_state.py proposal accept `
  --project-root "D:\novel" `
  --proposal-id "proposal-..." `
  --approval-id "approval-..." `
  --expected-canon-revision 7

python -X utf8 .\scripts\plot_state.py proposal retract `
  --project-root "D:\novel" `
  --proposal-id "proposal-..." `
  --approval-id "approval-..." `
  --expected-canon-revision 8 `
  --reason "章节重写"
```

`list-proposals / inspect-proposal / accept-proposal / reject-proposal / retract-proposal` 是同组顶层兼容命令。

初始化完成后的权威来源增删、修订和元数据变化使用独立来源清单生命周期：

```powershell
python -B -X utf8 .\scripts\plot_state.py source-manifest status `
  --project-root "D:\novel"

python -B -X utf8 .\scripts\plot_state.py source-manifest preview `
  --project-root "D:\novel" `
  --plan "D:\migration\source-manifest-plan.json" `
  --expected-canon-revision 7

python -B -X utf8 .\scripts\plot_state.py source-manifest propose `
  --project-root "D:\novel" `
  --plan "D:\migration\source-manifest-plan.json" `
  --expected-canon-revision 7 `
  --idempotency-key "source-manifest-20260720"
```

`status` 与 `preview` 在已有 continuity DB 上使用私有只读快照，不创建或迁移项目
数据库；数据库尚未建立时直接报告缺失。`propose` 只冻结 immutable proposal。
最终接受仍走通用 `proposal accept`，并消费宿主签发、操作名为
`accept_source_manifest` 的一次性 grant；completion receipt 保持原样。

初始化之后的世界级力量定义使用独立 PowerSpec 生命周期：

```powershell
python -B -X utf8 .\scripts\plot_state.py power-spec validate `
  --spec "D:\migration\power-spec.json"

python -B -X utf8 .\scripts\plot_state.py power-spec preview `
  --project-root "D:\novel" `
  --spec "D:\migration\power-spec.json" `
  --expected-canon-revision 11

python -B -X utf8 .\scripts\plot_state.py power-spec propose `
  --project-root "D:\novel" `
  --spec "D:\migration\power-spec.json" `
  --expected-canon-revision 11 `
  --idempotency-key "power-spec-foundation-v1"
```

`--spec`（兼容名 `--spec-json`）可接收内联 JSON、UTF-8 JSON 文件或 `-` 标准输入。
`validate` 不访问项目；`preview` 要求现有 continuity DB 与匹配的 accepted canon
revision，但不创建/迁移数据库、不注册实体、不保存 proposal。`propose` 在一次原子
事务内注册稳定实体并保存 immutable `power_spec_change` proposal，不签发 grant，
也不修改 accepted canon。最终接受继续走通用 `proposal accept`，消费宿主签发、
操作名为 `accept_power_spec` 的一次性 grant，并通过 canon revision CAS 后生成
accepted commit；随后使用 `replay` 和力量查询核验。

初始化命令可在目标项目尚无 `.plot-rag/config.json` 时运行：

```powershell
python -X utf8 .\scripts\plot_state.py init dry-run `
  --workspace-root "D:\works" `
  --project-root "D:\works\new-novel" `
  --mode new `
  --target-profile plot_ready `
  --seed "玄幻升级流"

python -X utf8 .\scripts\plot_state.py init start `
  --workspace-root "D:\works" `
  --project-root "D:\works\new-novel" `
  --mode hybrid `
  --source "D:\works\materials" `
  --idempotency-key "init-start-001"

python -X utf8 .\scripts\plot_state.py init advance `
  --workspace-root "D:\works" `
  --session-id "init-..." `
  --expected-session-revision 3 `
  --idempotency-key "init-advance-003"

python -X utf8 .\scripts\plot_state.py init answer `
  --workspace-root "D:\works" `
  --session-id "init-..." `
  --answers-file ".\answers.json" `
  --expected-session-revision 4 `
  --idempotency-key "init-answer-004"

python -X utf8 .\scripts\plot_state.py init inspect `
  --workspace-root "D:\works" `
  --session-id "init-..." `
  --view diff

python -X utf8 .\scripts\plot_state.py init propose `
  --workspace-root "D:\works" `
  --session-id "init-..." `
  --expected-session-revision 5 `
  --idempotency-key "init-propose-005"

python -X utf8 .\scripts\plot_state.py init apply `
  --workspace-root "D:\works" `
  --proposal-id "init-proposal-..." `
  --approval-id "approval-..." `
  --expected-canon-revision 0 `
  --idempotency-key "init-apply-001"

python -X utf8 .\scripts\plot_state.py init verify `
  --project-root "D:\works\new-novel" `
  --commit-id "commit-..."
```

另有 `init list` 和 `init cancel`。`init answer --answers-file -` 可从 stdin 读取一个 JSON object；`init dry-run --output FILE` 只有显式指定时才保存报告。

初始化数据库存储检查与显式迁移使用独立入口；省略 `--database` 时优先选择
`<workspace-root>/.plot-rag/init.sqlite3`，仅在 canonical 文件不存在且 legacy
文件已存在时回退到 `.plot-rag-init/init.sqlite3`：

```powershell
python -B -X utf8 .\scripts\plot_init_storage.py inspect `
  --workspace-root "D:\works\new-novel"

python -B -X utf8 .\scripts\plot_init_storage.py migrate `
  --database "D:\works\new-novel\.plot-rag\init.sqlite3" `
  --dry-run

python -B -X utf8 .\scripts\plot_init_storage.py migrate `
  --database "D:\works\new-novel\.plot-rag\init.sqlite3" `
  --backup `
  --cleanup-orphans `
  --compact
```

实际迁移强制创建并校验在线备份；`--compact` 在迁移事务提交后独立执行，
以免 VACUUM 失败回滚已经通过校验的数据迁移。inspect 与 dry-run 不创建目录、
数据库、WAL、SHM 或备份文件。

长篇命令：

```powershell
python -X utf8 .\scripts\plot_state.py longform refresh `
  --project-root "D:\novel" `
  --with-embeddings

python -X utf8 .\scripts\plot_state.py longform context `
  --project-root "D:\novel" `
  --prompt "设计第十二章章纲" `
  --artifact-stage outline `
  --chapter-no 12 `
  --max-context-chars 9000

python -X utf8 .\scripts\plot_state.py longform status --project-root "D:\novel"
python -X utf8 .\scripts\plot_state.py longform recover `
  --project-root "D:\novel" `
  --run-id "STALE_VECTOR_RUN_ID"
python -X utf8 .\scripts\plot_state.py longform benchmark
```

`longform index` 是 `longform refresh` 的别名；benchmark 不要求项目配置。`longform recover` 当前只重试 vector 投影，复用同一向量刷新器；已有成功 retry 会返回 `cached`，不会再次创建运行。

力量查询命令：

```powershell
python -X utf8 .\scripts\plot_state.py power systems `
  --project-root "D:\novel"

python -X utf8 .\scripts\plot_state.py power state `
  --project-root "D:\novel" `
  --mention "主角" `
  --chapter-no 120 `
  --knowledge-plane objective

python -X utf8 .\scripts\plot_state.py power path `
  --project-root "D:\novel" `
  --mention "主角" `
  --track-id "ent-..." `
  --target-rank-id "ent-..."

python -X utf8 .\scripts\plot_state.py power explain `
  --project-root "D:\novel" `
  --mention "主角" `
  --action-id use `
  --ability-id "ent-..." `
  --chapter-no 120 `
  --scene-index 2

python -X utf8 .\scripts\plot_state.py power compare `
  --project-root "D:\novel" `
  --left-mention "主角" `
  --right-mention "对手" `
  --conditions-json '{"environment":"狭窄地宫","preparation":"双方无预置阵法"}'
```

所有力量查询返回 canon revision、投影哈希、稳定实体解析结果、来源事件与
unknown/conflicted 项。`power compare` 只输出条件化矩阵，不替作者裁决胜负。
`--conditions-json` 可接收内联 JSON object、JSON 文件路径或 `-`（stdin）；
省略时使用空对象 `{}`。

v1.5 性能、抽取、体验与物品命令：

```powershell
python -X utf8 .\scripts\plot_state.py performance status --project-root "D:\novel"
python -X utf8 .\scripts\plot_state.py performance benchmark --project-root "D:\novel"
python -X utf8 .\scripts\plot_state.py performance compare `
  --left ".\baseline.json" `
  --right ".\candidate.json"

python -X utf8 .\scripts\plot_state.py extraction list --project-root "D:\novel"
python -X utf8 .\scripts\plot_state.py extraction inspect `
  --project-root "D:\novel" `
  --job-id "extraction-job-..."
python -X utf8 .\scripts\plot_state.py extraction retry `
  --project-root "D:\novel" `
  --job-id "extraction-job-..." `
  --expected-attempt-count 1

python -X utf8 .\scripts\plot_state.py experience propose `
  --project-root "D:\novel" `
  --contract ".\event-contract.json" `
  --expected-control-revision 0 `
  --idempotency-key "experience-propose-001"
python -X utf8 .\scripts\plot_state.py experience inspect `
  --project-root "D:\novel" `
  --contract-id "experience-contract-..."
python -X utf8 .\scripts\plot_state.py experience lock `
  --project-root "D:\novel" `
  --contract-id "experience-contract-..." `
  --expected-control-revision 1 `
  --idempotency-key "experience-lock-001"
python -X utf8 .\scripts\plot_state.py experience review `
  --project-root "D:\novel" `
  --review ".\experience-review.json" `
  --assistant-file ".\chapter-12.md" `
  --expected-control-revision 2 `
  --idempotency-key "experience-review-001"

python -X utf8 .\scripts\plot_state.py item definition --project-root "D:\novel" --definition-id "item-def-..."
python -X utf8 .\scripts\plot_state.py item instance --project-root "D:\novel" --instance-id "item-inst-..."
python -X utf8 .\scripts\plot_state.py item inventory --project-root "D:\novel" --mention "测试角色甲"
python -X utf8 .\scripts\plot_state.py item custody --project-root "D:\novel" --subject-type item_instance --subject-id "item-inst-..."
python -X utf8 .\scripts\plot_state.py item function --project-root "D:\novel" --function-id "item-fn-..." --instance-id "item-inst-..."
python -X utf8 .\scripts\plot_state.py item runtime --project-root "D:\novel" --instance-id "item-inst-..."
python -X utf8 .\scripts\plot_state.py item history --project-root "D:\novel" --instance-id "item-inst-..."
python -X utf8 .\scripts\plot_state.py item observations --project-root "D:\novel" --instance-id "item-inst-..."
```

v1.6 Advantage 与特殊物品命令：

```powershell
python -X utf8 .\scripts\plot_state.py advantage definition --project-root "D:\novel" --advantage-id "adv-example-core"
python -X utf8 .\scripts\plot_state.py advantage anchors --project-root "D:\novel" --advantage-id "adv-example-core"
python -X utf8 .\scripts\plot_state.py advantage runtime --project-root "D:\novel" --advantage-id "adv-example-core"
python -X utf8 .\scripts\plot_state.py advantage modules --project-root "D:\novel" --advantage-id "adv-example-core"
python -X utf8 .\scripts\plot_state.py advantage ledger --project-root "D:\novel" --advantage-id "adv-example-core"
python -X utf8 .\scripts\plot_state.py advantage knowledge --project-root "D:\novel" --advantage-id "adv-example-core"
python -X utf8 .\scripts\plot_state.py advantage progression --project-root "D:\novel" --advantage-id "adv-example-core"
python -X utf8 .\scripts\plot_state.py advantage exposure --project-root "D:\novel" --advantage-id "adv-example-core"
python -X utf8 .\scripts\plot_state.py special-item context --project-root "D:\novel" --advantage-id "adv-example-core"
```

Advantage 共八类 CLI 查询；`advantage anchor` 是 `advantage anchors` 的兼容别名。
`advantage knowledge`、`advantage anchors` 与 `special-item context|inventory` 支持
`--visibility generation|inspection|raw`，默认 `generation`；`inspection`/`raw`
必须显式指定，只用于审计、迁移和隔离验收。CLI/MCP 响应会回显实际 visibility，
Hook/Prepare 则始终显式使用 generation，避免 author-plan 或未揭示事实进入正文生成。
Anchor 的 generation 查询固定只返回 active + canon；`--include-inactive` 与
`--include-noncanon` 仅在显式 inspection/raw 下生效。

`performance compare` 接收两个 report JSON；`extraction retry` 使用 attempt-count CAS；
experience propose/lock/review 使用独立 control revision CAS。item 与 Advantage
命令均查询 accepted replay-derived projection，不签发 grant。

### CLI approval 边界

- 非交互调用必须显式传入已有 `--approval-id`，CLI 只消费该 grant。
- 未提供 `--approval-id` 时，只有真实 TTY 才能签发短时 grant。
- TTY 会要求连续两次输入**完整且完全相同的 proposal ID**；任一次不匹配即停止，且不签发、不消费、不写正典。
- CLI 没有 `--yes`，也没有非交互自动批准路径。
- `proposal accept`、`proposal retract` 和 `init apply` 使用同一规则。

### MCP

MCP server 为 `plot-rag-state`，工具分为十一组：

1. 剧情准备与兼容查询：`prepare_plot_turn`、`commit_plot_turn`、`propose_plot_turn`、`query_plot_state`、`query_plot_craft`、`get_plot_state`、`doctor_plot_rag`。
2. 生命周期：`list_plot_proposals`、`inspect_plot_proposal`、`reject_plot_proposal`、`accept_plot_proposal`、`retract_plot_proposal`、`query_plot_state_at`、`replay_plot_continuity`。
3. 来源清单：`get_source_manifest_status`、`preview_source_manifest_change`、`propose_source_manifest_change`。
4. 初始化：`start_story_initialization`、`dry_run_story_initialization`、`advance_story_initialization`、`answer_story_initialization`、`inspect_story_initialization`、`build_story_initialization_proposal`、`apply_story_initialization`、`verify_story_initialization`、`list_story_initializations`、`cancel_story_initialization`。
5. 长篇：`refresh_longform_index`、`recover_longform_projection`、`build_longform_context`、`get_longform_status`、`run_longform_benchmark`。
6. 力量体系：`validate_power_spec_change`、`preview_power_spec_change`、`propose_power_spec_change`、`list_power_systems`、`query_power_state`、`query_progression_path`、`explain_power_action`、`compare_power_conditions`。
7. 性能：`get_plot_performance_status`、`run_plot_performance_benchmark`、`compare_plot_prepare_paths`。
8. 抽取任务：`list_plot_extraction_jobs`、`inspect_plot_extraction_job`、`retry_plot_extraction_job`。
9. 事件体验：`propose_event_experience`、`inspect_event_experience`、`lock_event_experience`、`review_event_experience`。
10. 物品：`query_item_definition`、`query_item_instance`、`query_item_function`、`query_item_runtime`、`query_item_custody`、`query_actor_inventory`、`query_item_history`、`query_item_observations`。
11. Advantage：`query_advantage_definition`、`query_advantage_anchors`、`query_advantage_runtime`、`query_advantage_modules`、`query_advantage_ledger`、`query_advantage_knowledge`、`query_advantage_progression`、`query_advantage_exposure`、`query_special_item_context`。

只有经测试证明零写入的既有工具、来源清单 status/preview、PowerSpec
validate/preview、三个性能工具、
`list_plot_extraction_jobs`、`inspect_plot_extraction_job`、
`inspect_event_experience`、八个物品查询和九个 Advantage/特殊物品查询带 MCP
`readOnlyHint`。来源清单 propose、PowerSpec propose、
`retry_plot_extraction_job`、体验
propose/lock/review、可能创建/迁移派生 SQLite 或索引的其他查询、状态、验证和
longform status 工具不虚标只读。`accept_plot_proposal`、
`retract_plot_proposal` 和 `apply_story_initialization` 只能消费宿主已经签发的
grant；MCP server 不注册 grant issuer。

## 配置

推荐直接复制 [templates/config.v3.json](templates/config.v3.json)：

```powershell
New-Item -ItemType Directory -Force "D:\novel\.plot-rag" | Out-Null
Copy-Item ".\templates\config.v3.json" "D:\novel\.plot-rag\config.json"
$env:SILICONFLOW_API_KEY = "<TOKEN>"
```

config v3 的关键字段：

```json
{
  "config_version": 3,
  "grill": {
    "enabled": true,
    "schema_version": "plot-rag-intent/v1",
    "database_path": ".plot-rag/grill.sqlite3",
    "one_question_per_turn": true,
    "recommend_answer": true,
    "explore_project_first": true,
    "max_questions": 6,
    "session_ttl_seconds": 21600
  },
  "lifecycle": {
    "strict": true,
    "longform_context_chars": 7000
  },
  "performance": {
    "prepare_v2": {
      "enabled": false,
      "shadow": true,
      "single_read_snapshot": true,
      "exact_state_short_circuit": true,
      "batch_embedding": true,
      "batch_failure_fallback_single": true,
      "rerank_max_concurrency": 4,
      "remote_total_concurrency": 6,
      "singleflight": true,
      "persistent_exact_cache": true,
      "http_keep_alive": true
    },
    "extraction": {
      "mode": "sync",
      "async_shadow": true,
      "next_plot_turn_barrier": true,
      "barrier_requires_proposal_resolution": true,
      "deterministic_repairs": [
        "single_action_event_type_echo"
      ]
    }
  },
  "event_experience": {
    "enabled": true,
    "required_before_event_design": true,
    "event_seed_required": true,
    "receipt_hash_binding": true,
    "derive_from_intent": true,
    "grill_on_structural_ambiguity": true,
    "one_question_per_turn": true,
    "max_questions_per_chain": 1,
    "repeat_same_question_limit": 1,
    "session_ttl_seconds": 21600,
    "visible_in_story_artifacts": false
  },
  "items": {
    "schema_version": "plot-rag-item/v1",
    "delta_version": "plot-rag-delta/v4",
    "strict_runtime_validation": false,
    "power_binding_bridge": true,
    "readable_projection": true
  },
  "advantage": {
    "enabled": false,
    "shadow": true,
    "schema_version": "plot-rag-advantage/v1",
    "strict_runtime_validation": false,
    "readable_projection": true,
    "mandatory_context": true
  },
  "authority_sources": [
    {
      "glob": "正文/**/*.md",
      "role": "canon",
      "scope_policy": "infer_and_review",
      "ingest_policy": "include",
      "priority": 100
    }
  ],
  "initialization": {
    "schema_version": "auto",
    "proposal_only": true,
    "default_mode": "auto",
    "default_target_profile": "plot_ready",
    "default_interaction_profile": "balanced"
  },
  "power_system": {
    "mode": "auto",
    "schema_version": "plot-rag-power/v1",
    "strict_progression": true,
    "comparison_mode": "conditional",
    "unknown_policy": "quarantine",
    "profiles": []
  }
}
```

config、Intent Contract、EventExperienceContract、continuity state schema、item projection、Advantage projection、初始化协议、力量 schema 和 authority index schema 分别版本化。`config_version` 与兼容字段 `version` 只接受 JSON 整数 `1 / 2 / 3`，布尔值和浮点数会被拒绝，避免把 `true` 或 `1.0` 静默当成 v1。`initialization.schema_version=auto` 会在 v1/v2 间确定性协商；旧 `authority_globs` 会归一为带来源角色的 `authority_sources`；旧 state schema 在事务迁移前创建备份。`prepare_v2.batch_embedding=true` 是“允许使用批量路径”的能力开关，不代表每个 provider/model 都会批量；SiliconFlow `BAAI/bge-m3` 由 provider guard 强制 singleton-exact，并使用 `remote_total_concurrency` 预算派生有界并发。v1.6 的保守默认值不会直接切换执行语义：Prepare v2 shadow 只生成对照，legacy 路径仍是权威结果；Stop 仍同步抽取，async shadow proposal 不可 accept 且不进入 barrier；物品与 Advantage strict=false 只做 reducer dry-run/诊断。启用任一严格路径前必须先通过项目级等价、迁移与隔离作品验收。`grill` 块缺失时 config v3 使用上述默认值；若要保持升级前的直接执行体验，显式设置 `"grill": {"enabled": false}`。

## 硅基流动

默认模型：

- Embedding：`BAAI/bge-m3`
- Rerank：`BAAI/bge-reranker-v2-m3`
- 结构化抽取：`Qwen/Qwen3-30B-A3B-Instruct-2507`

三者默认共用 `SILICONFLOW_API_KEY`。兼容配置只接受插件专用环境变量名，不把密钥写入项目 JSON、日志、proposal、commit、snapshot 或错误信息。

SiliconFlow `api.siliconflow.cn + BAAI/bge-m3` 当前使用 singleton-exact query Embedding。原因是同一 query 在 singleton 与多输入 batch 中可能得到组成相关的向量差异；即使差异很小，也可能改变候选边界与最终 selected chunk。Prepare v2 因此不为该 provider/model 创建 batch provider，而是：

- 每条原子 need 独立发送 singleton 请求；
- 并发度由 `min(remote_total_concurrency, 4)` 限制，默认配置下最多为 `4`；
- 单条失败只降级对应 need，其他 need 按输入顺序稳定归并；
- Embedding cache identity 写入 `input_semantics=singleton_exact`，不与 `batch_independent` 结果混用；
- 遥测同时记录 `embedding_single_ms`（各请求耗时总和）与 `embedding_single_wall_ms`（并发墙钟）。

Rerank 仍按 need 有界并行，并使用进程级 4096 项 exact-result LRU 与 SingleFlight。缓存只接收已验证、不可变的 `(index, score)`；空结果、重复/越界 index、NaN/Inf、畸形 pair、provider 异常与被中断的 flight 都不会写入健康缓存。Rerank 后保留 provider 返回顺序，候选/lexical 阶段的固定 tie-break 不用于重新排列已经完成的 provider 排序。

剧情 Stop 抽取由 config v3 的 `remote.extract.enabled` 控制。初始化阶段的远端模型只承担**歧义复核**，默认关闭，必须由宿主显式开启：

```powershell
$env:PLOT_RAG_INIT_REMOTE_ENABLED = "true"
```

初始化始终先运行本地确定性 inventory、分类和 claim 抽取；只有低置信分类或本地没有 claim 时才调用远端复核。远端分类强制降为 `T4 / review / brainstorm`，远端 claim 强制为 `model_proposed / proposed / scope=null`，只能进入 proposal 和 provenance，不能直接晋升 current、timeless 或 accepted。bundle 顶层会记录 `remote_model_used`、extractor 版本、模型名与响应哈希，不记录凭据。

内置信任 `api.siliconflow.cn`、`api-inference.modelscope.cn` 和 `api.jina.ai`。其他服务域名必须由宿主环境显式加入：

```powershell
$env:PLOT_RAG_TRUSTED_HOSTS = "llm.example.com,127.0.0.1"
```

项目配置无权扩张可信主机名单。共享 `SILICONFLOW_API_KEY` 只允许发往 `api.siliconflow.cn`；非 loopback 服务必须使用 HTTPS；Embedding、Rerank、剧情抽取和初始化复核都阻断 HTTP redirect，避免 `Authorization` 被转发到第二个主机。

## 兼容与迁移

- config v1/v2 继续使用原有自动 prepare/commit 路径，避免已有项目升级即改变提交语义。
- config v3 强制 `lifecycle.strict=true`，`Stop` 只形成 proposal。
- v1.5 的 `performance.prepare_v2.enabled=false`、`performance.prepare_v2.shadow=true`、`performance.extraction.mode=sync` 与 `items.strict_runtime_validation=false` 是发行兼容默认值；Prepare/Stop 的 legacy 路径仍是权威结果，async shadow proposal 不可 accept/不阻断，v4 item proposal 也不可晋升。
- v1.4.0 的 config v3 默认启用 Grill；旧项目首次命中新创作请求时才会按需创建 `.plot-rag/grill.sqlite3`。
- 希望保持 v1.3.0 交互语义的项目可设置 `grill.enabled=false`；该开关不迁移、不删除也不读取 Grill 合同来改变正典。
- 升级前已有的活跃初始化 session 继续拥有最高优先级；新的初始化请求先锁定 Intent Contract，再在同轮 handoff。
- 续写只继承 TTL 内的 `COMPLETED` 合同；旧的未完成、`EXECUTING`、`HANDOFF_FAILED` 或损坏 Grill 状态不会自动变成执行授权。
- continuity schema v4 迁移到 v5 时先创建数据库备份，再从 immutable accepted events 重放力量投影。
- continuity schema v5 迁移到 v6 时同样先备份；v6 只增加控制层、抽取任务和物品投影表，不生成 accepted bootstrap event，不根据名称或 `attributes_json` 猜测功能。
- 旧 continuity projection hash 的定义保持冻结；物品表使用独立 `item_projection_hash`，可以单独 replay 和核验。
- v1.5 运行时继续提供冻结的 legacy 查询面；v1.4.3 会把 schema v6 识别为 `STATE_SCHEMA_TOO_NEW`，降级插件前必须恢复 schema-v5 备份。
- continuity schema v6 迁移到 v7 时生成 schema-v6 backup，只增量建立 Advantage 表与 metadata；continuity/item hash 保持冻结，Advantage 使用独立 `advantage_projection_hash`。
- v1.5 运行时会把 schema v7 识别为过新；降级前必须恢复 schema-v6 backup，并按 migration receipt 清理 `.plot-rag/物品/`、`.plot-rag/金手指/` 及其 staging/backup/lock。
- `plot-rag-delta/v3` 继续服务既有连续性与力量事件；schema v6 的新物品事件使用 `plot-rag-delta/v4`，legacy `inventory` 继续读取和重放。
- EventExperienceContract 使用独立 binding revision CAS，不推进 canon revision，也不进入 authority、FTS、向量、摘要或三层记忆；ExperienceReview revision 与 lifecycle binding 解耦。
- `InitializationBundle v1/v2` 不升版；显式物品标准化结果写入独立 `.plot-rag/items.v1.json`，仅有名称或持有人时继续保留 legacy inventory。
- Advantage 初始化结果写入 `.plot-rag/advantages.v1.json`；16 类 profile、来源 snapshot、provenance 与 package hash 共同锁定 sidecar 身份。
- initialization storage v1 继续可读；schema v2 只在新写入时使用 blob，历史行由 `plot_init_storage.py` 显式、备份优先地迁移和压缩。
- remote-review cache v1 行不会被 v2 身份复用；旧行按既有 TTL/容量策略自然淘汰。
- 旧 ability 事件映射到 ownership/runtime/use history；失去能力后的后续使用会被阻断。
- `InitializationBundle v1` 继续可读；带结构化力量模型的会话使用 v2。
- 新规则可在 `shadow` 模式先诊断，完成力量定义后再进入 strict。
- 旧 `authority_globs`、旧状态库和旧索引配置保留兼容读取或确定性迁移。
- 力量迁移见 [POWER_SYSTEM_MIGRATION.md](POWER_SYSTEM_MIGRATION.md)；v1.5 schema v6、item 与性能 rollout 见 [V1_5_MIGRATION.md](V1_5_MIGRATION.md)；v1.6 schema v7 与 Advantage 见 [V1_6_MIGRATION.md](V1_6_MIGRATION.md)。
- 引擎不导入、不修改，也不依赖 `webnovel-writer` 的 `.webnovel`、数据库或 Python 模块。

## 运行文件

- `.plot-rag/config.json`：项目配置。
- `.plot-rag/state.sqlite3`：receipts、proposal、grant hash、accepted commits、类型化事件，以及角色、关系、库存、时间、力量体系、schema-v6 物品投影和 schema-v7 Advantage 投影；event experience 与 extraction job 控制表不属于正典 replay。
- `.plot-rag/index.sqlite3`：兼容权威索引。
- `.plot-rag/authority.v1.sqlite3`：长篇 FTS5/BM25 权威索引。
- `.plot-rag/longform.v1.sqlite3`：三层记忆、三级摘要与项目方法模式。
- `.plot-rag/projection-runs.v1.sqlite3`：派生投影运行日志。
- `.plot-rag/grill.sqlite3`：非正典 Intent Contract、单问进度和 turn 幂等响应；可删除后重新澄清，不参与正典 replay。
- `.plot-rag/init.sqlite3`：初始化 session、journal、checkpoint 与冻结 proposal。
- `.plot-rag/items.v1.json`：初始化产生的物品 Definition/Instance/Function/Custody/Runtime/Observation sidecar；绑定来源 snapshot 与 package hash。
- `.plot-rag/advantages.v1.json`：初始化产生的 Advantage Definition/Anchor/Module/Runtime/Ledger/Knowledge/Contract/NarrativeContract sidecar。
- `.plot-rag/物品/`、`.plot-rag/金手指/`：从 accepted SQLite 状态重建的 disposable Markdown 投影。
- `.plot-rag/session-close-pending.json`：`Stop` 边界持久化的
  queued/running/failed/pending-review 屏障；accepted 与 no-delta 后清理。
- `.plot-rag/experience-review-diagnostics.jsonl`：自动体验审查失败的脱敏诊断；不改变 proposal、job、canon 或 lifecycle binding。
- `.plot-rag/commits/`：不可变提交物。
- `.plot-rag/state_snapshot.json`、`.plot-rag/continuity_snapshot.json`：可读派生快照。
- `.plot-rag/accepted-source-manifest.json`：已批准且绑定内容哈希的权威来源。
- `.plot-rag/completion-receipt.json`：初始化完成与 `BOOTSTRAP_READY` 验证收据。

## 验证


发行候选必须在同一份 staged payload 上完成验证。仓库内所有 E2E 输入均使用
合成 fixture；发布载荷不得包含单一作品的名称、角色、设定、真实来源清单、
开发机绝对路径、项目文件哈希或项目级验收报告。

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONWARNINGS='error::ResourceWarning'
python -B -X utf8 -m unittest discover -s tests -p "test_*.py"
python -B -X utf8 benchmarks\run_longform_benchmark.py validate
python -B -X utf8 benchmarks\run_longform_benchmark.py run
python -B -X utf8 benchmarks\run_v15_performance_benchmark.py validate
python -B -X utf8 scripts\release_gate.py validate --root .
python -B -X utf8 scripts\release_gate.py secrets --root . --history
python -B -X utf8 scripts\release_gate.py roundtrip --root .
python -B -X utf8 scripts\release_gate.py smoke --root .
git diff --check
```

版本、CLI、MCP 与 state-rag User-Agent 的 base version 必须一致。载荷定稿后使用
仓库包装命令生成唯一 cachebuster，再对生成后的同一 payload 重跑全部门禁：

```powershell
python -B -X utf8 scripts\release_gate.py cachebuster --root .
```

安装验证由调用方显式传入 source、marketplace 与 installed 路径：

```powershell
python -B -X utf8 scripts\release_gate.py verify-install `
  --source . `
  --marketplace <MARKETPLACE_JSON> `
  --installed <INSTALLED_PLUGIN_CACHE>
```
