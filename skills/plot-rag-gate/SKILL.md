---
name: plot-rag-gate
description: 在推演剧情、规划事件链、编写章纲、正文或初始化作品前，先用默认启用的单问 Grill 锁定八字段 Intent Contract，并为每个叙事事件锁定读者体验合同；再从同一 accepted 快照检索连续性、权威原文、剧情债务、三层记忆与网文方法。生成后只形成带逐字证据的 proposal，经一次性 grant 和 canon revision CAS 验收后再记录角色、关系、位置、物品、时间等类型化事件。也支持 new/ingest/hybrid 作品初始化。
---

# 剧情 RAG 门禁

## 总原则

插件启用后，剧情推进与作品初始化由 hook 主动仲裁。新创作请求先锁定用户意图；真正进入事件设计前，再锁定事件级读者体验；之后才进入项目 RAG、剧情 prepare 或初始化。不等待模型自行决定是否澄清、检索或写回。

第三方模型只负责 Embedding、Rerank、结构化抽取和候选生成。模型输出始终是 proposal；本地确定性代码独占 ID、schema、逐字证据、语义、作用域、所有权、CAS、幂等、grant 消费、正典写入和投影重放。

## 触发仲裁

按以下优先级处理：

1. 若存在 active initialization session，真实初始化回答、继续、inspect、propose、apply 或 cancel 由初始化接管；插件维护、分析、审查、测试、仓库和发布元任务保持静默。
2. 若存在 active Grill，本轮由 Grill 接管回答、重复、inspect、skip 或 cancel。
3. 若用户新发起初始化或真正要求推演、规划、写作剧情，先进入 Grill。
4. Grill 零问、完成或明确 skip 后，将锁定合同交给 `new / ingest / hybrid` 初始化；剧情任务先进入 EventSeed / Event Experience 门禁，再进入 prepare。
5. 元问题、功能说明、触发机制讨论、插件开发、初始化框架设计、否定、暂停、分析、审查和只读查询保持静默。

活跃初始化中的“继续”推进初始化，明确回答才消费当前初始化问题；插件维护、分析、审查、测试、仓库和发布元任务不改变初始化 revision。活跃 Grill 中的“继续”“开始吧”“下一步”等短续词只重复当前问题，不消费答案；“为什么问这个”“还剩几题”只查看状态。“按推荐答案”“你来定”才表示采用当前推荐。没有活跃 Grill 时，短续词继承 session transcript 中最近一条有效用户任务；剧情续写只继承 TTL 内状态为 `COMPLETED` 的同任务族合同，`AWAITING_ANSWER`、`EXECUTING` 和失败 handoff 不复用。显式“剧情推演”“推演下一章”始终优先。

## Grill Intent Contract

默认协议为 `plot-rag-intent/v1`，合同固定包含：

1. `problem_to_solve`
2. `expected_deliverable`
3. `reader_experience`
4. `protagonist_drive_conflict`
5. `scope_endpoint`
6. `success_criteria`
7. `hard_constraints`
8. `model_autonomy`

执行规则：

- `grill.enabled=true`、`one_question_per_turn=true`、`recommend_answer=true`、`explore_project_first=true`；
- 每轮只呈现一个问题；推荐答案必须附说明，且不能被“继续”默认为已接受；
- 先探测项目配置、authority 和连续性存储。角色状态、关系、位置、道具、力量状态、故事时间等可检索事实不向用户追问，合同锁定后由 RAG 读取；
- 输入已明确核心问题、交付物、范围并覆盖足够的体验、冲突、成功标准或约束时，使用零问快通道；
- 问题上限默认 `6`，会话 TTL 默认 `21600` 秒；达到上限时以标明来源的工作流默认值补齐；
- 明确 skip 使用 `grill_skip_default` 补齐缺项并立即 handoff；明确 cancel 结束本轮且不执行原任务；
- Grill 问答轮独占当前 turn，不创建剧情 receipt，`Stop` 必须抑制剧情 proposal 抽取；
- 状态保存在 `.plot-rag/grill.sqlite3`。它是项目本地、非正典、可丢弃控制状态，不进入 authority、初始化 bundle、连续性事件、accepted commit 或 replay；
- `grill.enabled=false` 保留升级前的直接初始化/剧情 prepare 兼容路径。

锁定后把八字段合同追加为 `[LOCKED_INTENT_CONTRACT]`，再交接对应工作流。项目事实继续由 accepted RAG 决定；合同只约束本轮目的、范围、成功标准和模型裁量边界。

## Event Experience Contract

Intent Contract 约束整轮任务；`plot-rag-event-experience/v1` 约束一个具体叙事事件。二者不可互相替代。

执行顺序固定为：

```text
locked Intent Contract
→ EventSeed
→ EventExperienceArc
→ locked EventExperienceContract
→ zero-remote manifest validation
→ plot receipt / retrieval / generation
→ ExperienceReview
```

- 每个受管事件先保存稳定 `EventSeed`，再一对一绑定 locked contract；多事件链另外保存 `EventExperienceArc`；
- 合同绑定非空 `source_intent_contract_id`、正整数 revision 和 lowercase SHA-256 hash；
- 合同至少说明进入读者状态、目标状态、主情绪、次情绪顺序、情绪转折、`entry/peak/exit` 强度、实现机制、知识位置、视角人物状态、兑现或揭示、余味、反体验和成功信号；
- 高置信度内容从锁定 Intent、accepted outline 与当前事件链确定性派生；只有结构性歧义才进入独立 `phase=event_experience` 单问；
- 同一事件链最多新增一个问题；“继续”不接受推荐项，无效答案最多原样重复一次；
- 缺少 locked contract 时保持 `suppress_plot_receipt=true`、`suppress_remote_retrieval=true`、`suppress_stop_proposal=true`；
- receipt、proposal、grant、accept 与异步 extraction job 必须携带同一组 seed、contract、Intent ID/revision/hash 和 manifest hash；
- control revision 与 canon revision 分离；supersession 保留 append-only lineage；
- `ExperienceReview` 只能引用 locked contract，并用 assistant 全文 SHA-256、连续逐字证据和半开区间 offset 记录实际体验与合同漂移；
- EventSeed、Arc、Contract、Question、Review 与 extraction job 属于控制面，不进入 authority、FTS、向量、摘要、三层记忆或正典 replay。

## config v3 严格剧情闭环

1. `UserPromptSubmit` 先完成 Grill；只有 Intent Contract 已锁定，才生成 EventSeed 与体验弧。
2. Event Experience 门禁必须先得到 locked manifest；缺合同的轮次零 receipt、零远端检索。
3. prepare 固定 canon revision、active projection hash 和单一 accepted read snapshot，再注入：
   - `receipt_id` 与 `prepared_canon_revision`；
   - EventSeed、EventExperienceContract 与 Intent 的 ID、revision 和 hash；
   - accepted current、timeless、planned、historical 与显式 branch facts；
   - 权威原文和来源角色；
   - working、episodic、semantic memory；
   - 章、事件弧和卷摘要；
   - 活跃 open loop；
   - 当前任务相关的网文方法卡。
4. 模型只使用注入的 accepted 事实推进剧情；缺失关键事实时，把需求拆为一至五条原子查询。
5. Prepare v2 使用 provider-aware exact Embedding：只有已验证 batch-independent 的 provider/model 才批量；SiliconFlow `BAAI/bge-m3` 强制 singleton-exact，并在全局远端预算内最多四路并发、逐 need 故障隔离、按输入序归并。Rerank 采用有界并行、exact LRU/SingleFlight，并保留 provider 返回顺序。
6. 已 handoff 的剧情轮在 `Stop` 同步抽取，或登记绑定完整身份的 durable extraction job；初始化、Grill 和体验问答轮直接抑制抽取。
7. 下一剧情轮先检查同 branch/sequence barrier；queued、running、failed 或 pending-review 均不得越过。
8. 本地 validator 校验类型、置信度、连续逐字证据、实体端点、移动起点、库存/物品守恒、关系维度、时间顺序和 stage/scope 规则。
9. Stop 或 worker 只保存 immutable proposal，不更新 accepted event、current、timeless 或权威来源。
10. proposal 由用户或可信宿主审阅；只有一次性 approval grant 与 `expected_canon_revision` 同时通过，accept 才生成 immutable accepted commit。
11. accepted commit 触发精确投影、快照、FTS5 索引、三级摘要、三层记忆和向量投影；各投影失败时独立 retry/replay，不重新抽取 proposal。

config v1/v2 继续使用旧版自动 commit 行为。v1.5 保守默认值为 `performance.prepare_v2.enabled=false`、`performance.prepare_v2.shadow=true`、`performance.extraction.mode=sync`、`performance.extraction.async_shadow=true`、`items.strict_runtime_validation=false`、`event_experience.enabled=true`。Prepare v2 shadow 只对照，legacy 结果仍是权威；async shadow proposal 不可 accept 且不进入 barrier；item strict=false 只做 reducer dry-run/诊断，v4 accept 在 grant 消费前阻断。不要把 shadow、迁移试点或尚未通过项目级验收的严格路径写成默认权威结果。

## 正典晋升规则

- stage 不明确时按 `brainstorm/proposed` fail-closed。
- accepted `brainstorm` 不进入权威 current。
- accepted `outline` 只进入 `planned`。
- accepted `draft` 只进入对应 branch 的 provisional facts。
- 只有 accepted `bootstrap / final / published` 可以更新权威 current。
- accepted `timeless` 进入独立 timeless projection，并在有效状态查询中合并。
- `historical` 与插叙事件保留历史，不覆盖当前时间线。
- reject、retract、correction 和 supersession 保留不可变审计链，并通过 replay 重建结果。
- grant 必须绑定 proposal、stage、branch、chapter、artifact revision、canon revision、授权操作、目标路径与有效期。
- MCP、hook、普通非交互 CLI 和远端模型没有 grant 签发权。
- 相同 accept 网络重试返回原 commit；已消费 grant 用于不同请求必须失败。
- CAS 冲突后重新 prepare、抽取和审阅，不采用最后写入者获胜。

## 类型化连续性

使用稳定实体 ID 和别名，不以自由文本重复创建角色、地点、道具或势力。

重点记录：

- 角色状态、目标、伤势、能力、代价、冷却和限制；
- 多维关系、势力归属、承诺、债务和认知边界；
- 原子移动事件与有效位置区间；
- legacy 库存获得、转移、消耗、遗失、数量和唯一所有权；
- 物品定义、实例、堆叠、法律所有权、物理保管、容器、运行态、功能、使用史和观察；
- 结构化章节、场景、故事时间、有效区间与因果链接；
- open loop 的创建、升级、期限、兑现与关闭；
- correction、supersession、retraction 和 branch。

关系必须区分目标角色和关系维度；legacy 库存必须区分具体物品；唯一物品的 title、custody、container 和 location 分别守恒；角色离开某地的证据不得把该地写成当前位置。

## 通用物品功能门禁

continuity schema v6 在 legacy `inventory_state` 之外增加独立、可重放的 `plot-rag-item/v1` 投影：

```text
ItemDefinition
→ ItemFunctionDefinition / ItemFunctionBinding
→ ItemInstance / ItemStack
→ ownership / custody / containment
→ item runtime / function runtime
→ use history / observation
→ item_projection_hash
```

- accepted immutable item event 是唯一权威；所有物品状态表和可读文件均为 replay 投影；
- 物品定义、具体实例和同质堆叠分开；异质批次先 split lot，再进行数量变化；
- 名义所有权与实际保管分开；携带者、保管者、容器和地点不得混成一个 holder 字段；
- 功能只能来自 accepted definition 或显式 binding，不根据名称、题材常识或 legacy `attributes_json` 猜测；
- 普通效果由 item function 拥有；力量效果通过 Ability/PowerBinding bridge 复用，effect、cost、cooldown 只能有一个 owner；
- item use 先校验 custody/ownership、功能解锁、充能、耐久、冷却、资格、位置、力量绑定和知识平面，再由本地 reducer 原子计算 before/after；
- 战斗、解谜、逃生、生产、治疗、交易、权限和证据任务把相关 item definition、function、custody 与 runtime 放入 mandatory context；
- 第三方模型只输出 mention、显式动作、显式变化、故事坐标和连续逐字证据，不生成稳定 ID、current/remaining 或其他派生计数；
- 新 item 事件使用顶层仅含 `schema_version + deltas` 的 `plot-rag-delta/v4` mixed envelope；非物品事件继续使用冻结的 v3 字段与语义；
- 旧 `plot-rag-delta/v3`、legacy `inventory`、旧查询与既有 continuity projection hash 保持兼容；新表使用独立 `item_projection_hash`。
- strict=false 时 v4 只做 dry-run 与 warning diagnostics，authority accept 以 `ITEM_STRICT_RUNTIME_DISABLED` 阻断；legacy v3 inventory 旁路兼容。`power_binding_bridge=false` 阻断新 bridge；true 只复用既有 AbilityDefinition。
- `readable_projection=true` 时 accept/retract/replay 原子刷新 `.plot-rag/物品/` Markdown 树；发布失败只降级可读投影，不改写 accepted canon。

## 特殊物品与金手指门禁

continuity schema v7 在普通物品与力量投影之外增加独立的
`plot-rag-advantage/v1` Advantage 投影，用于记录系统、传承、知识、时间、
契约、血脉、空间、器灵和其他主角叙事优势：

```text
AdvantageDefinition
→ AdvantageAnchor
→ AdvantageModuleDefinition
→ runtime / ledger / knowledge / contract
→ progression / exposure
→ advantage_projection_hash
```

- accepted immutable Advantage event 是唯一权威；定义、锚点、模块、运行态、
  账本、五层知识、契约、成长、暴露和 `.plot-rag/金手指/` 均由 replay 重建；
- `Stop` 的远端抽取继续使用顶层仅含 `schema_version + deltas` 的
  `plot-rag-delta/v4` mixed envelope，并按原顺序拆成冻结 legacy v3 delta、
  Item v4 candidate 与 Advantage v4 candidate 三族。Advantage candidate 使用闭字段
  `event_type/action/subject/objects/changes/scope/story_coordinate/knowledge_plane/confidence/evidence`，
  只有 `effective_at/ambiguity` 可选；
- 第三方模型只提交原文中的 mention、显式动作、显式变化、故事坐标、知识面、
  confidence 和连续逐字证据；不得提交 stable ID、EventExperience identity、
  `before/after/current/remaining/resulting/computed/derived` 状态或其他本地派生量；
- 本地 adapter 先读取同一 accepted snapshot，解析既有 Advantage、anchor、
  module、knowledge、contract、Item、Ability 与实体。合法的 define/create
  candidate 才能按 `artifact_id + reference_type + normalized mention`
  确定性生成稳定 ID；引用歧义或缺失保持结构化 issue，不猜测绑定；
- 批量 adapter 按原顺序保留独立合法事件，并为失败项写入
  `candidate_index + adapter_stage`；任何 error issue 都使本轮 proposal
  进入 quarantined/pending-review，不把部分结果直接晋升正典；
- 每个可形成事实的 Advantage leaf event 必须由本地绑定一个 locked
  EventExperience tuple：
  `experience_contract_id + experience_contract_hash + event_seed_id + event_seed_revision`。
  单合同可覆盖同一事件组；多合同按
  `story_coordinate` 分组并与 manifest 的 `dependency_order` 一一对应。
  数量、坐标、合同 ID 或 correction wrapper/leaf 不一致时 fail-closed；
- proposal 保存前强制非空 `experience_contract_id`；签发 grant 与 accept
  时重新验证该 ID 属于 locked manifest，并同时复核 Intent、EventSeed、
  contract hashes、control revision、artifact identity 和 canon revision。
  校验失败不消费 grant；
- `advantage_spec / advantage_anchor / advantage_module / advantage_bind /
  advantage_activate / advantage_trigger / advantage_use / advantage_reward /
  advantage_cost / advantage_upgrade / advantage_reveal / advantage_contract /
  advantage_correction` 均进入同一 proposal → grant → accept → replay 闭环；
- outline/planned、historical、branch 与 current 分开投影；generation
  默认隐藏 `author_plan`、未揭示模块和非当前分支。`inspection/raw` 只用于
  显式审计；
- `advantage anchors`（别名 `advantage anchor`）与
  `query_advantage_anchors` 只读查询锚点、载体、所有者、绑定状态和转移规则；
  generation 强制只返回 active + canon，审计过滤只有显式
  `inspection/raw` 才可放宽。

## 力量体系门禁

统一力量内核使用 `plot-rag-power/v1`，并由 12 个声明式 profile 适配修仙、
魔法、技能树、游戏、武道、异能、血脉、科技、契约召唤、系统流、混合体系和
无超凡作品。

始终区分：

- 定义：体系、成长轨、阶段、晋升边、能力、资源、克制和桥接规则；
- 持有：角色通过天赋、功法、职业、装备、血脉、契约、职位或系统获得什么；
- 可用：当前资源、冷却、状态、地点、资格和来源绑定是否满足；
- 使用：某个故事坐标上实际发动、消耗、失败或产生后果的事件；
- 观察：人物或读者看见的效果，不自动扩张为客观完整机制。

力量事件包括 `power_spec / progression / resource / ability / status_effect /
power_binding / qualification / power_observation`。strict Stop 统一输出
`plot-rag-delta/v3` typed delta，本地再执行实体类型、证据、晋升边、前置、资源、
冷却、故事坐标、来源、资格、转换和知识平面校验。

世界级定义变化必须单独使用 `proposal_kind=power_spec_change`，并消费
`accept_power_spec` grant。普通 `story_delta` 只保存角色运行态与观察。
无 accepted `BridgeRule/ConversionRule` 时保持体系隔离，不推导跨体系数值等价。

初始化完成后需要整批补录或修订力量定义时，使用独立 PowerSpec 入口：

```text
validate
→ preview(expected canon revision)
→ immutable power_spec_change proposal
→ trusted host accept_power_spec grant
→ canon CAS
→ accepted commit
→ replay
```

validate 不访问项目；preview 要求现有 continuity DB 和匹配的 accepted revision，
但不创建/迁移数据库、不注册实体、不保存 proposal；propose 只在一个原子事务中
注册稳定实体并冻结 proposal，不签发 grant，也不修改 accepted canon。独立入口只
接收世界级定义；角色当前境界、能力持有、资源、状态、资格与观察继续走
`story_delta`。

战斗、突破、训练、装备、系统奖励和契约任务必须注入力量 mandatory context。
比较结果保持条件化多维矩阵，不生成默认单一战力值或无条件胜者。

## 作品初始化

### 模式

- `new`：按“题材合同 → 世界因果核 → 人物锚点 → 剧情发动机 → 连载兑现合同”从零创建。
- `ingest`：只读 inventory、分类、claim、归一、冲突、缺口和标准化，不自动补全缺失内容。
- `hybrid`：先 ingest，再只补阻塞目标档位的缺口。
- `auto`：只负责路由，持久化 session 最终必须是 `new / ingest / hybrid`。

目标档位为 `plot_ready / world_bible / normalize_only / continuity_ready`，交互档位为 `minimal / balanced / deep`。

初始化 schema 使用 `auto` 协商：通用项目保持 `plot-rag-init/v1`；出现结构化
力量 claim、显式 profile 或力量对象时使用 `plot-rag-init/v2`。v2 在原有世界、
人物、剧情和来源字段之外保存 `PowerSpec` 与角色力量 bootstrap。mundane profile
保持无超凡语义，不生成伪境界链。

### 初始化安全链

```text
初始化请求
→ Grill 单问 / 零问快通道
→ 锁定 plot-rag-intent/v1
→ start / dry-run
→ inventory / genre-world-plot discovery
→ checkpoint / answer / advance
→ normalized InitializationBundle
→ frozen proposal
→ host approval grant
→ init apply
→ typed bootstrap commit
→ staging materialize
→ accepted source manifest
→ state/index/snapshot/summary/vector projections
→ verify
→ COMPLETED + BOOTSTRAP_READY
```

- `dry-run` 零 session、零数据库、零 canon、零配置和零源文件写入。
- start、advance、answer、inspect 和 propose 都是非正典阶段。
- 原始资料只读；文件名和目录名只提供来源角色候选，不自动获得正典身份。
- 客观真相、人物认知、公开叙事、读者已知和作者计划分层保存。
- `unknown / conflicted / deferred` 是合法状态，不用模型猜测填平。
- checkpoint/resume 使用 session revision CAS 和幂等键；source/canon 漂移会使 proposal stale。
- frozen proposal 必须包含 source manifest、provenance、冲突、缺口、决策、标准文件清单、逐文件 diff、apply plan 和 package hash。
- apply 只消费宿主签发的初始化 grant，并验证 proposal、target root、source hash、target old/new hash 和 canon revision。
- 完成 materialize 与投影验证后，把 session 标记为 `COMPLETED`，设置 `BOOTSTRAP_READY`；完成会话不再占用初始化 hook。
- 首次初始化请求先经过 Grill；零问、合同完成或明确 skip 后，在同一轮 handoff 给初始化 start。初始化 session 一旦 active，后续初始化轮次由它优先接管，不重复盘问；无关元任务保持静默且不推进 session。
- 初始化目录从剧情 hook、authority discovery、长篇索引和 Stop 抽取中硬排除。
- `InitializationBundle v1/v2` 保持原样兼容；初始化提取到的物品模型固定写入独立 `.plot-rag/items.v1.json` sidecar，不引入 init v3。
- 只有显式来源支持的功能、唯一性和运行态进入 sidecar；仅有名称/持有人时继续保留 legacy inventory，单次效果证据继续保存为 observation。

世界初始化使用最小可运行世界 MVW、九类底层对象、十三个运行模块、`kernel → regional → local → scene → texture` 五级分辨率和十项压力测试。只扩展当前剧情能触达或必须支撑的分辨率，不在动笔前机械填满百科。

## 长篇召回门禁

事实优先级：

1. accepted 精确状态、active facts 与 timeless facts；
2. accepted append-only typed events；
3. accepted source manifest 绑定的权威原文；
4. accepted 三层记忆和三级摘要；
5. provisional branch facts，仅在显式请求对应 branch 时使用；
6. 网文方法卡，只组织推演，不作为事实；
7. Embedding 与 Rerank 只负责排序，不拥有事实裁决权。

检索规则：

- FTS5/BM25 索引只读取 accepted authority source；
- `.git/**`、`.plot-rag/**`、`.plot-rag-init/**` 和越界 symlink/junction 硬排除；
- 未变化文件基于内容 SHA-256 跳过解析、分块和 Embedding；
- 当前状态和活跃剧情债务必须拥有 mandatory context 配额；
- Prepare v2 对整轮 accepted 数据使用一个 read snapshot；结束前复核 canon revision 与 projection hash，漂移则丢弃并重跑；
- accepted 精确状态可本地短路，但明确要求原文证据时仍执行 authority retrieval；
- query Embedding 按 provider/model 语义选择 `batch_independent` 或 `singleton_exact`；SiliconFlow `api.siliconflow.cn + BAAI/bge-m3` 不创建 batch provider，而是按 `min(remote_total_concurrency, 4, pending needs)` 有界并发。批响应错位或部分失败时逐 need 回退，所有结果按输入 need 序归并；
- Rerank 只做有界并行，完成后保持 provider 返回顺序；固定 `score/priority/path/ordinal` tie-break 只用于 candidate/lexical 阶段；
- SingleFlight、精确缓存和连接复用只优化相同身份输入。Embedding cache key 必须区分 `batch_independent` 与 `singleton_exact`；Rerank 使用进程级 4096-entry LRU，并把 provider identity、模型、exact query、ordered documents、`top_n` 和归一化版本纳入 key；
- 未显式声明 `cache_identity` 的 Rerank provider 只允许同一对象命中；失败、被中断、空结果、畸形 pair、重复/负数/越界 index、NaN/Inf score 均不缓存，失败 flight 必须清理后允许健康重试；
- `INDEX_UNAVAILABLE`、远端错误、降级检索和单次空结果都不是 `MISS_CONFIRMED`；
- 只有确切、健康、覆盖充分的查询才能确认缺失；
- 只对可再生缓存做有界保留，accepted commit 和正典事件不可裁剪。

## 网文方法活用

同时使用通用剧情方法目录和 `knowledge/webnovel_methods.json` 网文方法包。

- 先识别题材、产物阶段、任务和连续性风险，再选少量真正相关的方法。
- 网文方法包括章首承接、章末钩子、悬念兑现窗口、爽点蓄压与释放、升级资源账、能力代价、群像轮转、重复度控制和卷末高潮。
- 把方法落实为当前人物的目标、对立行动、合理行动、预期外结果、困难选择、状态变化和后续问题。
- 只从 accepted 章节学习项目级成功模式；rejected 或 provisional draft 不进入 craft memory。
- 方法与权威事实冲突时始终服从权威事实。
- 不在用户可见的正文、章纲或大纲中复述方法名、检查表或内部工作流。

## MCP 工具

剧情与查询：

- `prepare_plot_turn`
- `propose_plot_turn` / 兼容名 `commit_plot_turn`
- `query_plot_state`
- `query_plot_craft`
- `get_plot_state`
- `doctor_plot_rag`

生命周期：

- `list_plot_proposals`
- `inspect_plot_proposal`
- `reject_plot_proposal`
- `accept_plot_proposal`
- `retract_plot_proposal`
- `query_plot_state_at`
- `replay_plot_continuity`

来源清单：

- `get_source_manifest_status`
- `preview_source_manifest_change`
- `propose_source_manifest_change`

初始化：

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

长篇：

- `refresh_longform_index`
- `recover_longform_projection`
- `build_longform_context`
- `get_longform_status`
- `run_longform_benchmark`

力量体系：

- `validate_power_spec_change`
- `preview_power_spec_change`
- `propose_power_spec_change`
- `list_power_systems`
- `query_power_state`
- `query_progression_path`
- `explain_power_action`
- `compare_power_conditions`

性能与抽取：

- `get_plot_performance_status`
- `run_plot_performance_benchmark`
- `compare_plot_prepare_paths`
- `list_plot_extraction_jobs`
- `inspect_plot_extraction_job`
- `retry_plot_extraction_job`

事件体验：

- `propose_event_experience`
- `inspect_event_experience`
- `lock_event_experience`
- `review_event_experience`

物品：

- `query_item_definition`
- `query_item_instance`
- `query_item_function`
- `query_item_runtime`
- `query_item_custody`
- `query_actor_inventory`
- `query_item_history`
- `query_item_observations`

特殊物品与金手指：

- `query_advantage_definition`
- `query_advantage_anchors`
- `query_advantage_runtime`
- `query_advantage_modules`
- `query_advantage_ledger`
- `query_advantage_knowledge`
- `query_advantage_progression`
- `query_advantage_exposure`
- `query_special_item_context`

`compare_power_conditions` 只返回查询时派生的条件化 `ComparisonClaim`：
`derivation=query_time`、`persisted=false`。它引用 accepted 定义、运行态、知识平面
和逐事件证据，不写入正典，也不保存无条件胜负结论。

MCP 只消费已有 `approval_id`，不签发 grant。除既有零写入工具外，来源清单
status/preview、PowerSpec validate/preview、三个性能工具、`list_plot_extraction_jobs`、
`inspect_plot_extraction_job`、`inspect_event_experience`、八个物品查询和九个
Advantage/特殊物品查询带 `readOnlyHint`；来源清单 propose、
PowerSpec propose、
`retry_plot_extraction_job`、体验 propose/lock/review 与所有正典写操作不标只读。
可能创建或迁移派生库的其他查询/状态工具同样不虚标只读。

## CLI

统一入口为：

```powershell
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" --help
```

顶层命令：

- `prepare`：生成带 `prepared_canon_revision` 的收据和长篇上下文。
- `propose`：从最终 assistant 文本抽取 proposal；`commit` 是兼容名，config v3 下仍不自动 accept。
- `query / query-at`：查询 accepted 连续性；`query-at` 支持章节、场景、scope 和 branch。
- `craft / dump / doctor`：方法召回、兼容状态读取和统一零写入诊断。
- `replay`：确定性重建 accepted 投影。
- `source-manifest status|preview|propose`：只读检查或冻结初始化后的 accepted
  来源清单变化；接受继续使用通用 proposal 生命周期和
  `accept_source_manifest` grant。
- `power-spec validate|preview|propose`：纯编译、零写入预览或冻结初始化后的
  世界级力量定义；接受继续使用通用 proposal 生命周期和
  `accept_power_spec` grant。
- `migrate --component all|config|state [--dry-run]`：备份并迁移配置或状态 schema，写入迁移/回滚收据。

来源清单命令：

```powershell
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" source-manifest status --project-root "<PROJECT_ROOT>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" source-manifest preview --project-root "<PROJECT_ROOT>" --plan "<PLAN_JSON_OR_FILE>" --expected-canon-revision 7
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" source-manifest propose --project-root "<PROJECT_ROOT>" --plan "<PLAN_JSON_OR_FILE>" --expected-canon-revision 7 --idempotency-key "<KEY>"
```

`status` 与 `preview` 只读取已有 continuity DB 的私有快照；DB 尚未建立时不创建。
`propose` 只保存 proposal，不签发 grant，也不更新 accepted manifest。

PowerSpec 命令：

```powershell
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" power-spec validate --spec "<JSON_OR_FILE_OR_STDIN>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" power-spec preview --project-root "<PROJECT_ROOT>" --spec "<JSON_OR_FILE_OR_STDIN>" --expected-canon-revision 11
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" power-spec propose --project-root "<PROJECT_ROOT>" --spec "<JSON_OR_FILE_OR_STDIN>" --expected-canon-revision 11 --idempotency-key "<KEY>"
```

`--spec-json` 是兼容名，`-` 表示从 stdin 读取。validate/preview 均为零项目写入；
propose 原子保存 `power_spec_change` proposal，不签发/消费 grant。随后由宿主签发
`accept_power_spec` grant，再使用通用 proposal accept 完成 canon CAS。

v1.5 诊断与控制命令：

```powershell
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" performance status --project-root "<PROJECT_ROOT>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" performance benchmark --project-root "<PROJECT_ROOT>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" performance compare --left "<BASELINE_REPORT>" --right "<CANDIDATE_REPORT>"

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" extraction list --project-root "<PROJECT_ROOT>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" extraction inspect --project-root "<PROJECT_ROOT>" --job-id "<JOB_ID>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" extraction retry --project-root "<PROJECT_ROOT>" --job-id "<JOB_ID>" --expected-attempt-count 1

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" experience propose --project-root "<PROJECT_ROOT>" --contract "<CONTRACT_JSON_OR_FILE>" --expected-control-revision 0 --idempotency-key "<KEY>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" experience inspect --project-root "<PROJECT_ROOT>" --contract-id "<CONTRACT_ID>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" experience lock --project-root "<PROJECT_ROOT>" --contract-id "<CONTRACT_ID>" --expected-control-revision 1 --idempotency-key "<KEY>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" experience review --project-root "<PROJECT_ROOT>" --review "<REVIEW_JSON_OR_FILE>" --assistant-file "<ASSISTANT_FILE>" --expected-control-revision 2 --idempotency-key "<KEY>"

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" item definition --project-root "<PROJECT_ROOT>" --definition-id "<DEFINITION_ID>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" item instance --project-root "<PROJECT_ROOT>" --instance-id "<INSTANCE_ID>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" item inventory --project-root "<PROJECT_ROOT>" --mention "<ACTOR>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" item custody --project-root "<PROJECT_ROOT>" --subject-type item_instance --subject-id "<INSTANCE_ID>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" item function --project-root "<PROJECT_ROOT>" --function-id "<FUNCTION_ID>" --instance-id "<INSTANCE_ID>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" item runtime --project-root "<PROJECT_ROOT>" --instance-id "<INSTANCE_ID>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" item history --project-root "<PROJECT_ROOT>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" item observations --project-root "<PROJECT_ROOT>"

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" advantage definition --project-root "<PROJECT_ROOT>" --advantage-id "<ADVANTAGE_ID>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" advantage anchors --project-root "<PROJECT_ROOT>" --advantage-id "<ADVANTAGE_ID>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" advantage runtime --project-root "<PROJECT_ROOT>" --advantage-id "<ADVANTAGE_ID>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" advantage modules --project-root "<PROJECT_ROOT>" --advantage-id "<ADVANTAGE_ID>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" advantage ledger --project-root "<PROJECT_ROOT>" --advantage-id "<ADVANTAGE_ID>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" advantage knowledge --project-root "<PROJECT_ROOT>" --advantage-id "<ADVANTAGE_ID>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" advantage progression --project-root "<PROJECT_ROOT>" --advantage-id "<ADVANTAGE_ID>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" advantage exposure --project-root "<PROJECT_ROOT>" --advantage-id "<ADVANTAGE_ID>"
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" special-item context --project-root "<PROJECT_ROOT>" --advantage-id "<ADVANTAGE_ID>"
```

所有 Advantage 查询支持 `--visibility generation|inspection|raw`，默认
`generation`。`advantage anchors` 的 `--include-inactive` 与
`--include-noncanon` 只在显式 `inspection/raw` 下生效；`advantage anchor`、
`advantage module`、`special-item inventory` 和顶层 `special-item-context`
分别是相应只读入口的兼容别名。

具体写操作参数以 `--help` 为准；inspect/list/query 命令不得被当作 grant issuer。

proposal 命令：

```powershell
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" proposal list `
  --project-root "<PROJECT_ROOT>" `
  --canon-status proposed

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" proposal inspect `
  --project-root "<PROJECT_ROOT>" `
  --proposal-id "<PROPOSAL_ID>"

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" proposal accept `
  --project-root "<PROJECT_ROOT>" `
  --proposal-id "<PROPOSAL_ID>" `
  --approval-id "<APPROVAL_ID>" `
  --expected-canon-revision 7

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" proposal reject `
  --project-root "<PROJECT_ROOT>" `
  --proposal-id "<PROPOSAL_ID>" `
  --reason "<REASON>" `
  --idempotency-key "<KEY>"

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" proposal retract `
  --project-root "<PROJECT_ROOT>" `
  --proposal-id "<PROPOSAL_ID>" `
  --approval-id "<APPROVAL_ID>" `
  --expected-canon-revision 8 `
  --reason "<REASON>"
```

顶层 `list-proposals / inspect-proposal / accept-proposal / reject-proposal / retract-proposal` 是兼容别名。

初始化命令：

```powershell
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" init dry-run `
  --workspace-root "<WORKSPACE_ROOT>" `
  --project-root "<PROJECT_ROOT>" `
  --mode auto `
  --target-profile plot_ready `
  --seed "<SEED>"

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" init start `
  --workspace-root "<WORKSPACE_ROOT>" `
  --project-root "<PROJECT_ROOT>" `
  --mode auto `
  --source "<SOURCE>" `
  --idempotency-key "<KEY>"

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" init advance `
  --workspace-root "<WORKSPACE_ROOT>" `
  --session-id "<SESSION_ID>" `
  --expected-session-revision 3 `
  --idempotency-key "<KEY>"

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" init answer `
  --workspace-root "<WORKSPACE_ROOT>" `
  --session-id "<SESSION_ID>" `
  --answers-file "<JSON_FILE>|-" `
  --expected-session-revision 4 `
  --idempotency-key "<KEY>"

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" init inspect `
  --workspace-root "<WORKSPACE_ROOT>" `
  --session-id "<SESSION_ID>" `
  --view summary

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" init propose `
  --workspace-root "<WORKSPACE_ROOT>" `
  --session-id "<SESSION_ID>" `
  --expected-session-revision 5 `
  --idempotency-key "<KEY>"

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" init apply `
  --workspace-root "<WORKSPACE_ROOT>" `
  --proposal-id "<PROPOSAL_ID>" `
  --approval-id "<APPROVAL_ID>" `
  --expected-canon-revision 0 `
  --idempotency-key "<KEY>"

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" init verify `
  --project-root "<PROJECT_ROOT>" `
  --commit-id "<COMMIT_ID>"
```

另有 `init list / cancel`。`init start / dry-run` 不要求目标项目预先存在 `.plot-rag/config.json`。

长篇命令：

```powershell
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" longform refresh `
  --project-root "<PROJECT_ROOT>" `
  --with-embeddings

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" longform context `
  --project-root "<PROJECT_ROOT>" `
  --prompt "<TASK>" `
  --artifact-stage outline `
  --chapter-no 12

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" longform status `
  --project-root "<PROJECT_ROOT>"

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" longform recover `
  --project-root "<PROJECT_ROOT>" `
  --run-id "<STALE_VECTOR_RUN_ID>"

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" longform benchmark
```

`longform index` 是 `longform refresh` 的别名。`longform recover` 只接管精确
`run_id` 指向的 ownerless 或已确认 owner 死亡的派生 vector 运行，并复用同一
向量刷新器执行 retry；live 或无法验证的 owner 保持 fail-closed。已有成功
retry 时返回 `cached`，不创建重复运行，也不改变正典或 grant。

力量查询命令：

```powershell
python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" power systems `
  --project-root "<PROJECT_ROOT>"

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" power state `
  --project-root "<PROJECT_ROOT>" `
  --mention "<ACTOR>" `
  --chapter-no 12

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" power path `
  --project-root "<PROJECT_ROOT>" `
  --mention "<ACTOR>" `
  --track-id "<TRACK_ID>" `
  --target-rank-id "<RANK_ID>"

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" power explain `
  --project-root "<PROJECT_ROOT>" `
  --mention "<ACTOR>" `
  --action-id use `
  --ability-id "<ABILITY_ID>"

python -B -X utf8 "$env:CLAUDE_PLUGIN_ROOT\scripts\plot_state.py" power compare `
  --project-root "<PROJECT_ROOT>" `
  --left-mention "<ACTOR_A>" `
  --right-mention "<ACTOR_B>" `
  --conditions-json "<JSON_OBJECT>"
```

CLI approval 规则：

- 非交互调用只消费显式 `--approval-id`。
- 未提供 `--approval-id` 时，必须是真实 TTY，并连续两次输入完整、完全相同的 proposal ID，才会签发短时 grant。
- 任一次输入不匹配时不签发、不消费、不执行 accept/retract/apply。
- 所有 parser 都没有 `--yes`。

## 硅基流动与密钥

默认模型：

- Embedding：`BAAI/bge-m3`
- Rerank：`BAAI/bge-reranker-v2-m3`
- 结构化抽取：`Qwen/Qwen3-30B-A3B-Instruct-2507`

三者默认共用 `SILICONFLOW_API_KEY`。兼容配置只允许 `EMBED_API_KEY`、`PLOT_RAG_EMBED_API_KEY`、`RERANK_API_KEY`、`PLOT_RAG_RERANK_API_KEY`、`PLOT_RAG_LLM_API_KEY` 等插件专用名称。密钥不得写入项目 JSON、日志、proposal、commit、snapshot、completion receipt 或错误信息。

SiliconFlow `BAAI/bge-m3` 的 query Embedding 固定使用 singleton-exact。`batch_embedding=true` 只是允许 provider guard 选择批量路径，不覆盖此判定。遥测必须同时保留各 singleton 请求耗时之和与并发墙钟；Rerank 的 cache hit、miss、SingleFlight wait 和最大并发分别记录，不能折叠成一个模糊的 cache 指标。

Live Chat smoke 必须通过语义门禁：固定 fixture 只能产生一条目标角色到目标地点的 `movement` delta，`action=arrive/enter`、`field=current`、`value={}`、confidence 为 `(0,1]` 内有限数，evidence 是 assistant 原文中同时锚定角色与地点的逐字子串，且没有 `story_coordinate`、skipped 或额外 delta。HTTP 成功、非空 JSON 或“至少一条 delta”都不构成通过；smoke 只读，不写 continuity。

初始化歧义复核默认关闭，必须由宿主设置 `PLOT_RAG_INIT_REMOTE_ENABLED=true`。本地确定性分类与 claim 抽取始终先运行；只有低置信分类或零 claim 才调用远端。远端分类强制为 `T4/review/brainstorm`，远端 claim 强制为 `model_proposed/proposed/scope=None`，只能进入 proposal 与 provenance。

除内置 SiliconFlow、ModelScope、Jina 外，任何自定义服务域名都必须由宿主进程的 `PLOT_RAG_TRUSTED_HOSTS` 显式信任；项目配置不能扩张该名单。共享 `SILICONFLOW_API_KEY` 只允许发往 `api.siliconflow.cn`，非 loopback 必须 HTTPS，所有携带凭据的远端请求阻断 redirect。

## 状态解释

- `AWAITING_ANSWER`：Grill 正等待当前单问答案；本轮不创建剧情 receipt。
- `AWAITING_EVENT_EXPERIENCE`：EventSeed 已冻结，但体验合同尚未锁定；零 receipt、零远端检索。
- `EXECUTING`：Intent Contract 已锁定并 handoff，但对应执行尚未完成；续写不得复用。
- `COMPLETED`：Grill handoff 已完成；TTL 内的剧情 continuation 可继承该合同。
- `CANCELLED / HANDOFF_FAILED`：本轮 Grill 已取消或交接失败；不作为续写合同。
- `prepared`：已生成绑定 canon revision 的检索收据。
- `proposed`：已冻结候选，但正典未变化。
- `queued / running`：durable extraction job 尚未形成 proposal；对应下一剧情轮 barrier 保持阻断。
- `pending_review`：抽取已形成 proposal，但尚未明确 accept/reject/retract；barrier 继续阻断。
- `accepted`：grant、CAS 和本地门禁通过，已生成 accepted commit。
- `rejected / retracted`：保留审计记录，不进入或已从有效投影撤销。
- `completed / BOOTSTRAP_READY`：初始化文件、事件和投影均已验证。
- `degraded`：精确本地状态仍可用，但某个远端或派生投影失败；不得确认缺失。
- `skipped`：未命中剧情、无 prepared receipt 或没有可提交事实。
- `failed`：配置、抽取、校验、grant、CAS、事务或投影失败；不得把该轮视为已写回。

插件只借鉴 `webnovel-writer` 的 proposal、accepted commit、派生投影、事件重放、实体消歧和分层记忆思想，不导入或修改它的 `.webnovel`、数据库或源码。
