# plot-rag-gate v1.6.4 迁移指南

本指南覆盖 `v1.5.0 / continuity schema v6 / item projection v1` 升级到
`v1.6.4 / continuity schema v7 / Advantage projection v1`。其中 v1.6.4
把 extraction 屏障持久化并入宿主支持的 `Stop` Hook，v1.6.2 增加初始化后的
独立 PowerSpec validate/preview/propose 生命周期，v1.6.1 增加 accepted
source-manifest 生命周期；历史上的
v5→v6 物品迁移仍以 [V1_5_MIGRATION.md](V1_5_MIGRATION.md) 为准；本文件只描述
特殊物品、金手指、Advantage v1、来源清单与独立 PowerSpec 导入的增量升级。

Advantage 是独立于普通物品和力量体系的叙事优势投影。物理载体继续引用
`item_instance` 或 `item_stack`，能力继续通过既有 PowerBinding 接入；系统、传承、
知识、时间、契约、血脉和社交类优势则由 Advantage 自己记录定义、锚点、模块、
运行态、账本、认知层、契约、叙事承诺、成长阶段和暴露风险。

## 1. 迁移前不变量

- 停止会写入目标项目的 Hook、CLI、MCP、worker 与编辑器自动任务；
- 记录插件版本、Git commit、config hash、state DB hash、canon revision、
  continuity projection hash、item projection hash 与关键 legacy 查询结果；
- 真实项目先复制到隔离目录，完成 dry-run、迁移、replay、查询和回滚演练；
- `.plot-rag/`、SQLite、WAL/SHM、迁移 receipt、可读投影、性能报告和密钥均不加入
  Git；
- `SILICONFLOW_API_KEY` 只从宿主环境读取；
- 迁移不得根据“炉、戒指、系统、血脉”等名称推断金手指功能，也不得把计划能力、
  传闻或误解晋升为正典。

## 2. v1.6 配置合同

config v3 新增：

```json
{
  "advantage": {
    "enabled": false,
    "shadow": true,
    "schema_version": "plot-rag-advantage/v1",
    "strict_runtime_validation": false,
    "readable_projection": true,
    "mandatory_context": true
  }
}
```

默认含义：

- `enabled=false`：不把 Advantage 严格路径设为 authority；
- `shadow=true`：可构建、校验、回放和比较 Advantage 投影，但不自动接受正典；
- `strict_runtime_validation=false`：候选只做本地 reducer dry-run 和诊断；
- `readable_projection=true`：从 accepted SQLite 投影重建
  `.plot-rag/金手指/`；
- `mandatory_context=true`：剧情触及金手指时，将已接受的定义、模块、运行态、账本、
  认知、成长与风险上下文放入同一 accepted 快照。

v1/v2→v3 配置迁移会补齐上述默认值，同时保留合法自定义字段。布尔值必须是严格
JSON boolean，schema version 必须是 `plot-rag-advantage/v1`。

## 3. continuity schema v6 → v7

### 3.1 Dry-run

```powershell
python -B -X utf8 .\scripts\plot_state.py migrate `
  --project-root "<PROJECT_ROOT>" `
  --component state `
  --dry-run
```

Dry-run 只检查数据库所有权、源版本和迁移计划，不创建备份、receipt、表、sidecar
或可读投影。

### 3.2 执行

```powershell
python -B -X utf8 .\scripts\plot_state.py migrate `
  --project-root "<PROJECT_ROOT>" `
  --component state
```

写迁移继续使用 `BEGIN IMMEDIATE`、源文件身份复核、SQLite online backup、完整性
检查、内容哈希与原子发布。v7 以 additive DDL 增加：

- `advantage_definitions`
- `advantage_anchors`
- `advantage_module_definitions`
- `advantage_runtime_slots`
- `advantage_runtime_state`
- `advantage_ledger`
- `advantage_knowledge`
- `advantage_contracts`
- `advantage_narrative_contracts`
- `advantage_projection_meta`

备份位于：

```text
<PROJECT_ROOT>/.plot-rag/backups/
  state.sqlite3.schema-v6.source-<SOURCE_HASH>.bak
```

迁移不会新增 accepted commit、continuity event 或 canon revision；空 Advantage
投影也是合法、可重复 replay 的状态。

### 3.3 迁移 receipt

state migration receipt 必须同时记录：

- `new_sha256`
- `item_projection_hash`
- `advantage_projection_hash`
- `schema_status`
- schema-v6 backup 路径
- rollback 的 `expected_current_sha256`
- rollback 的 `expected_item_projection_hash`
- rollback 的 `expected_advantage_projection_hash`
- `readable_projection_cleanup`

`readable_projection_cleanup` 明确列出以下派生文件；恢复 schema-v6 backup 后应清理，
而不是把 v7 Markdown 留给旧运行时读取：

```text
.plot-rag/物品/
.plot-rag/.item-readable-backup
.plot-rag/.item-readable-stage-*
.plot-rag/.item-readable.lock
.plot-rag/金手指/
.plot-rag/.advantage-readable-backup
.plot-rag/.advantage-readable-stage-*
.plot-rag/.advantage-readable.lock
```

这些文件均是 disposable projection，不参与正典或 projection hash。

## 4. 三套独立哈希

v1.6 保持三个互不混合的投影身份：

| 投影 | 哈希 | 兼容规则 |
| --- | --- | --- |
| legacy continuity | `projection_hash` | 输入、排序和语义保持冻结 |
| item | `item_projection_hash` | v6 item reducer 独立 replay |
| Advantage | `advantage_projection_hash` | 前缀固定为 `advantage_projection_` |

迁移与 replay 至少验证：

1. continuity hash 未因 v7 DDL 变化；
2. item hash 未因 Advantage 表变化；
3. Advantage hash 在相同 accepted 事件序列上可重复；
4. branch、correction、retraction 和 supersession 不串入其他分支；
5. `planned|rumor|misread` 不进入当前 canon runtime；
6. objective、actor belief、public、reader 和 author 五层知识不互相泄漏。

## 5. 初始化 sidecar 与 profile

标准初始化 sidecar：

```text
<PROJECT_ROOT>/.plot-rag/advantages.v1.json
```

canonical schema：

```text
schemas/plot-rag-advantage/v1.schema.json
  -> ../plot-rag-advantage.v1.json
```

sidecar 固定绑定：

- `plot-rag-advantage/v1`
- work ID
- 原初始化 schema
- source snapshot hash
- 16 类 profile 注册表及其 hash
- definitions、anchors、modules、runtime slots
- runtime/ledger bootstrap
- knowledge、contracts、narrative contracts
- provenance
- package hash

只有逐字来源支持的事实进入强类型数组。仅有名称、持有人或题材常识时保持
unmodeled；物理锚点只引用 Item stable ID，不复制物品定义；能力只建立桥接，不复制
PowerSpec。

## 6. Stop 自动候选与逐事件体验绑定

v1.6 strict lifecycle 继续使用顶层仅含 `schema_version + deltas` 的
`plot-rag-delta/v4` mixed envelope，并保持输入顺序拆成三族：冻结的 legacy v3
delta、Item v4 neutral candidate、Advantage v4 neutral candidate。Advantage 保留
独立 typed event schema：

```text
assistant text
→ remote neutral Advantage candidate
→ local reference resolution / deterministic stable ID
→ local EventExperience tuple binding
→ immutable proposal
→ host grant + canon revision CAS
→ accepted commit
→ Advantage replay / readable projection
```

远端 candidate 是闭字段对象：

```text
event_type
action
subject={kind,mention}
objects=[{role,mention}]
changes
scope
story_coordinate={calendar_id,ordinal}
knowledge_plane
confidence
evidence
```

只有 `effective_at` 和 `ambiguity` 可选。远端不得提交：

- `advantage_id`、`anchor_id`、`module_id`、`knowledge_id`、contract ID 或其他
  stable ID；
- `experience_contract_id`、contract hash、EventSeed identity 或 lifecycle
  manifest identity；
- `before/after/current/remaining/resulting/computed/derived` 状态、计数或
  reducer 输出。

本地 adapter 的迁移不变量：

1. 先从同一 accepted SQLite snapshot 解析现有 Advantage、anchor、module、
   knowledge、contract、Item、Ability 与实体引用；
2. 只有通过 neutral candidate validator 的 define/create 事件，才按
   `artifact_id + reference_type + normalized mention` 生成确定性 stable ID；
3. 引用 unresolved/ambiguous、非法 role/action/change、缺故事坐标或远端注入
   计算字段时生成结构化 issue；批量结果保留独立合法事件，并在每个 issue 中记录
   `candidate_index + adapter_stage`；
4. lifecycle-bound proposal 中，每个可形成事实的 Advantage leaf event 都必须
   由本地写入完整
   `experience_contract_id + experience_contract_hash + event_seed_id + event_seed_revision`
   tuple；远端给出的体验 identity 不被接受；
5. locked manifest 只有一个合同，所有同组 candidate 绑定该合同；多个合同时，
   candidate 先按 `story_coordinate` 分组，合同按 `dependency_order` 排序，再
   一一绑定。坐标、数量或顺序关系不能确定时 fail-closed；
6. save 阶段缺绑定报告 `ADVANTAGE_EXPERIENCE_CONTRACT_REQUIRED`；外部合同 ID、
   correction wrapper 与 replacement leaf 不一致或 locked manifest 漂移报告
   `ADVANTAGE_EXPERIENCE_CONTRACT_MISMATCH`；
7. grant 与 accept 在事务中重新验证 Intent、EventSeed、合同 hash、control
   revision、artifact identity、canon revision 和逐事件合同成员关系；失败不消费
   grant，不修改 accepted canon；
8. legacy proposal、初始化 sidecar、无 lifecycle identity 的兼容调用与直接
   reducer fixture 不被 retroactive 强制绑定。

迁移验收必须使用真实 Stop 主链，而不是只把预构造 Advantage typed event 直接交给
service：至少覆盖 `prepare → extract → adapt → proposal → grant → accept → replay`，
并检查运行态、账本、知识面、planned/current 隔离和二次 hash。

## 7. CLI 与 MCP 验证

### 7.1 CLI

```powershell
python -B -X utf8 .\scripts\plot_state.py advantage definition `
  --project-root "<PROJECT_ROOT>" --advantage-id "<ADVANTAGE_ID>"

python -B -X utf8 .\scripts\plot_state.py advantage anchors `
  --project-root "<PROJECT_ROOT>" --advantage-id "<ADVANTAGE_ID>"

python -B -X utf8 .\scripts\plot_state.py advantage runtime `
  --project-root "<PROJECT_ROOT>" --advantage-id "<ADVANTAGE_ID>"

python -B -X utf8 .\scripts\plot_state.py advantage modules `
  --project-root "<PROJECT_ROOT>" --advantage-id "<ADVANTAGE_ID>"

python -B -X utf8 .\scripts\plot_state.py advantage ledger `
  --project-root "<PROJECT_ROOT>" --advantage-id "<ADVANTAGE_ID>"

python -B -X utf8 .\scripts\plot_state.py advantage knowledge `
  --project-root "<PROJECT_ROOT>" --advantage-id "<ADVANTAGE_ID>"

python -B -X utf8 .\scripts\plot_state.py advantage progression `
  --project-root "<PROJECT_ROOT>" --advantage-id "<ADVANTAGE_ID>"

python -B -X utf8 .\scripts\plot_state.py advantage exposure `
  --project-root "<PROJECT_ROOT>" --advantage-id "<ADVANTAGE_ID>"

python -B -X utf8 .\scripts\plot_state.py special-item context `
  --project-root "<PROJECT_ROOT>" --advantage-id "<ADVANTAGE_ID>"
```

`advantage anchor`、`advantage module`、`special-item inventory` 与
`special-item-context` 是相应只读入口的兼容名。所有 Advantage 查询与 combined
context 支持 `--visibility generation|inspection|raw`；默认固定为 `generation`。
`advantage anchors` 另外支持 `--include-inactive` 与 `--include-noncanon`，但
generation 模式始终强制 `active_only=true, include_noncanon=false`。
`inspection`/`raw` 只允许调用方显式选择，适合审计、迁移和隔离验收，不应直接注入
正文生成。Hook/Prepare 始终显式使用 `generation`。

### 7.2 MCP

以下工具必须存在且标记 `annotations.readOnlyHint=true`：

- `query_advantage_definition`
- `query_advantage_anchors`
- `query_advantage_runtime`
- `query_advantage_modules`
- `query_advantage_ledger`
- `query_advantage_knowledge`
- `query_advantage_progression`
- `query_advantage_exposure`
- `query_special_item_context`

九个 Advantage/特殊物品工具的 input schema 使用同一闭集
`generation|inspection|raw`，默认 `generation`；MCP 响应回显实际
`visibility`，防止审计调用和生成调用混淆。`query_advantage_anchors` 的
`include_inactive/include_noncanon` 仅在显式 `inspection/raw` 下扩展结果。

### 7.3 初始化后的来源清单变更

初始化 completion receipt 保持不可变。后续文件增删、正文修订、来源角色或
`indexable` 等 metadata 变化，统一通过：

```text
source-manifest preview
→ source-manifest propose
→ trusted host grant(accept_source_manifest)
→ proposal accept + canon revision CAS
→ replay / longform refresh / doctor / init verify
```

CLI：

```powershell
python -B -X utf8 .\scripts\plot_state.py source-manifest status `
  --project-root "<PROJECT_ROOT>"

python -B -X utf8 .\scripts\plot_state.py source-manifest preview `
  --project-root "<PROJECT_ROOT>" `
  --plan "<PLAN_JSON_OR_FILE>" `
  --expected-canon-revision 7

python -B -X utf8 .\scripts\plot_state.py source-manifest propose `
  --project-root "<PROJECT_ROOT>" `
  --plan "<PLAN_JSON_OR_FILE>" `
  --expected-canon-revision 7 `
  --idempotency-key "<KEY>"
```

MCP 对应：

- `get_source_manifest_status`：只读；
- `preview_source_manifest_change`：只读；
- `propose_source_manifest_change`：只创建 proposal。

status/preview 对已有 `state.sqlite3` 创建私有只读副本，不触碰源 DB、WAL 或 SHM；
DB 尚未建立时保持零写入并报告缺失。proposal 必须保留 normalized
`frozen_plan`；grant 与 accept 会从当前 physical active ledger、authoritative
snapshot 和目标文件字节重新执行 deterministic preview，再逐字段核对 base、
operations、IDs、action、counts 与 hashes。revision 和计数使用严格 JSON integer，
不接受 boolean、float 或 numeric string。只允许撤回当前最新的来源清单迁移；
不得直接编辑 accepted manifest 表或复制隔离 SQLite 覆盖正式项目。

### 7.4 初始化后的独立 PowerSpec 导入

完整 `plot-rag-power/v1` 定义包不再需要重新启动初始化 v2。迁移链固定为：

```text
power-spec validate
→ power-spec preview(expected canon revision)
→ power-spec propose
→ trusted host grant(accept_power_spec)
→ proposal accept + canon revision CAS
→ replay
→ power systems/path 查询
→ doctor / init verify
```

CLI：

```powershell
python -B -X utf8 .\scripts\plot_state.py power-spec validate `
  --spec "<JSON_OR_FILE_OR_STDIN>"

python -B -X utf8 .\scripts\plot_state.py power-spec preview `
  --project-root "<PROJECT_ROOT>" `
  --spec "<JSON_OR_FILE_OR_STDIN>" `
  --expected-canon-revision 11

python -B -X utf8 .\scripts\plot_state.py power-spec propose `
  --project-root "<PROJECT_ROOT>" `
  --spec "<JSON_OR_FILE_OR_STDIN>" `
  --expected-canon-revision 11 `
  --idempotency-key "<KEY>"
```

`--spec-json` 是 `--spec` 的兼容名；参数可为内联 JSON、UTF-8 JSON 文件，或用
`-` 从 stdin 读取。MCP 对应：

- `validate_power_spec_change`：纯编译、无项目访问，`readOnlyHint=true`；
- `preview_power_spec_change`：绑定现有 accepted revision，零写入，
  `readOnlyHint=true`；
- `propose_power_spec_change`：原子注册稳定实体并保存 immutable proposal，不标
  read-only。

PowerSpec package 使用
`plot-rag-lifecycle/power-spec-package-v1`，固定
`proposal_kind=power_spec_change`、`required_operation=accept_power_spec`、
`scope=timeless`、`artifact_stage=bootstrap`，并同时携带规范化
`power_package_hash` 与完整 lifecycle `package_hash`。validate/preview 不创建项目、
不创建或迁移 SQLite、不注册实体、不保存 proposal；propose 不签发/消费 grant，
不更新 accepted canon。接受阶段必须重新检查 grant、proposal 身份与
`expected_canon_revision`，CAS 竞争中只有一方晋升；失败方不得留下半注册实体或
半个 proposal。

独立导入只保存世界级定义。`actor_power_bootstrap` 或等价的角色当前境界、持有、
资源、状态、资格和观察必须继续走 `story_delta`；迁移工具不得把 planned/runtime
事实混入 timeless PowerSpec。

## 8. 可读投影

accepted Advantage 状态可重建为：

```text
.plot-rag/金手指/
  金手指索引.md
  定义/
  模块/
  运行态/
```

发布使用项目内锁、staging 与原子替换。可读 Markdown 只用于人工检查和本地检索；
SQLite 与 accepted event 才是权威源。写入失败只产生脱敏诊断，不回滚 accepted
canon。

## 9. 分阶段启用

1. 保持 `enabled=false, shadow=true`，运行 schema、sidecar、replay 和查询测试；
2. 在隔离项目中比较 continuity/item/Advantage 三个 hash；
3. 按 profile 启用 fixture，先验收 inheritance、resource transformer、
   growth relic、pocket domain、companion mentor；
4. 验收 strict Stop 三族顺序拆分、Advantage neutral candidate、本地 stable ID、
   逐事件 EventExperience contract ID/hash + EventSeed ID/revision tuple 与
   mandatory context；
5. 验收 Anchor CLI/MCP、其他 Hook/CLI/MCP、source/package/install cache 和
   Windows/Linux；
6. 真实作品 6/6 场景与 Stop 自动写回 E2E 均通过后，再按项目打开 strict authority。

## 10. 回滚到 v1.5

1. 停止所有目标项目进程；
2. 保存失败后的数据库、receipt、日志和诊断报告；
3. 核验 schema-v6 backup 的 SQLite 完整性与 SHA-256；
4. 核验当前数据库仍等于 receipt 的 `expected_current_sha256`；
5. 恢复 schema-v6 backup 为 `state.sqlite3`，不要让 v1.5 直接打开 schema v7；
6. 不混用迁移后的 WAL/SHM；
7. 按 receipt 清理 item/Advantage readable projection、stage、backup 和 lock；
8. 恢复旧 config；`.plot-rag/advantages.v1.json` 可移出运行目录留存，不据此生成
   v1.5 正典；
9. 重新运行 v1.5 doctor、legacy query、continuity replay 与 item replay；
10. 核对 continuity hash、item hash、canon revision、commit/event 数量和六类
    legacy query。

回滚不删除 accepted source、Git 文件、accepted commit、immutable event、初始化
原始来源或人工设定。


## 11. 发行验证

发行验证只依赖仓库内合成 fixture 和调用方显式提供的临时项目。不得把任一作品的
名称、角色、设定、来源清单、绝对路径、文件统计或项目哈希写入插件载荷。

```powershell
python -B -X utf8 -m unittest discover -s tests -p "test_*.py"
python -B -X utf8 .\scripts\release_gate.py validate --root .
python -B -X utf8 .\scripts\release_gate.py secrets --root . --history
python -B -X utf8 .\scripts\release_gate.py roundtrip --root .
python -B -X utf8 .\scripts\release_gate.py smoke --root .
```

版本定稿后运行 `release_gate.py cachebuster --root .`，再对同一 staged payload
重复上述命令。安装缓存由 `verify-install` 比较 tracked payload、哈希、重解析点、
ADS 与字节码残留。
