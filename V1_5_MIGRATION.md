# plot-rag-gate v1.5.0 迁移指南

本指南覆盖 config v3 项目从 v1.4.3 / continuity schema v5 升级到
v1.5.0 / continuity schema v6。v1.5 是增量升级：既有 accepted events、
canon revision、legacy `inventory_state`、初始化 bundle v1/v2 与旧查询语义继续
保留。这里的“继续保留”指 v1.5 运行时继续提供冻结的 legacy surface；v1.4.3
运行时会把 schema v6 识别为 `STATE_SCHEMA_TOO_NEW`，降级插件前必须恢复
schema-v5 备份。

## 1. 迁移前不变量

- 先停止会写入目标项目的 Hook、CLI、MCP 与后台 worker；
- 不把 `.plot-rag/`、SQLite、WAL/SHM、备份、receipt、性能报告或密钥加入 Git；
- 记录当前插件版本、Git commit、config hash、state DB hash、canon revision、
  continuity projection hash 与关键 legacy 查询结果；
- 对真实项目先使用隔离副本完成 dry-run、迁移、replay 和回滚演练；
- `SILICONFLOW_API_KEY` 只保存在宿主环境，不写入迁移参数或报告。

## 2. 保守发行默认值

升级配置时保持：

```json
{
  "performance": {
    "prepare_v2": {
      "enabled": false,
      "shadow": true
    },
    "extraction": {
      "mode": "sync",
      "async_shadow": true
    }
  },
  "event_experience": {
    "enabled": true,
    "required_before_event_design": true
  },
  "items": {
    "schema_version": "plot-rag-item/v1",
    "delta_version": "plot-rag-delta/v4",
    "strict_runtime_validation": false
  }
}
```

Prepare、抽取和物品默认值只增加 shadow、控制面和查询能力：旧 Prepare 路径仍是
权威结果，Stop 仍同步形成 proposal；async shadow proposal 不可 accept 且不进入
barrier。物品 strict=false 只做 reducer dry-run 与 warning diagnostics，v4
authority accept 在 grant 消费前以 `ITEM_STRICT_RUNTIME_DISABLED` 阻断；
legacy v3 inventory 继续兼容。

Event Experience 例外：它在 v1.5 config v3 中默认就是事件设计前的硬门禁。
`enabled=true` 且 `required_before_event_design=true` 时，受管 EventSeed 缺少 locked
contract、accepted outline 身份漂移或 manifest/hash 不一致，都会在零 receipt、零
远端调用处阻断。需要临时只观察控制结果时，显式设置
`event_experience.required_before_event_design=false`；完全关闭则显式设置
`event_experience.enabled=false`。

## 3. v5 → v6 数据库升级

### 3.1 Dry-run

```powershell
python -B -X utf8 .\scripts\plot_state.py migrate `
  --project-root "<PROJECT_ROOT>" `
  --component state `
  --dry-run
```

Dry-run 用于检查目标配置、数据库版本与迁移计划，不应修改正典。

### 3.2 执行

```powershell
python -B -X utf8 .\scripts\plot_state.py migrate `
  --project-root "<PROJECT_ROOT>" `
  --component state
```

写迁移由 `ContinuityStore.ensure_schema()` 统一执行：

1. 以 `BEGIN IMMEDIATE` 获得迁移互斥；
2. 锁定并反复核验同一源数据库文件身份；
3. 使用 SQLite online backup 生成一致备份；
4. 对备份执行完整性检查并校验内容哈希；
5. 在一个事务中增量创建 v6 表、控制表和独立 item projection 元数据；
6. 校验 v5 immutable surface 未被改写；
7. 提交后切回正常 WAL 模式并再次核验备份身份。

备份位于：

```text
<PROJECT_ROOT>/.plot-rag/backups/
  state.sqlite3.schema-v5.source-<SOURCE_HASH>.bak
```

迁移遇到未来 schema、缺表、数据库身份漂移、备份发布冲突或完整性失败时保持
fail-closed。

## 4. v6 增量内容

v6 新增或扩展：

- 物品定义、实例、堆叠、功能定义与绑定；
- 法律所有权、物理保管、携带、容器与地点；
- 物品运行态、功能运行态、使用史和观察；
- `EventSeed`、`EventExperienceArc`、`EventExperienceContract`、
  `ExperienceReview`、grandfather observed-only review 与单问控制记录；
- durable extraction job、租约、重试和 barrier 状态；
- Prepare v2 精确缓存与性能遥测。

Event Experience 与 extraction job 虽可与连续性表共用数据库文件，仍属于控制面：
它们不推进 canon revision，也不进入 authority、FTS、向量、摘要、三层记忆或
正典 replay。ExperienceReview 使用自己的 review revision；记录、失败或
supersede review 都不推进 lifecycle binding revision，也不改变 proposal、job 或
canon 状态。

## 5. Projection hash 兼容

- v1.5 冻结既有 continuity projection hash 的输入、排序与语义；
- 迁移不得把新 item 表混入旧 projection hash；
- 物品投影使用独立 `item_projection_hash` 与
  `plot-rag-item-projection/v1` 元数据；
- replay 必须分别核对 legacy continuity hash 与 item hash；
- 无物品事件时，空 item projection 仍是合法、可重放状态。

因此 v1.5 的 legacy 查询面继续返回原有 continuity hash；理解 schema v6 的 v1.5
客户端另外读取 `item_projection_hash`。这不表示 v1.4.3 可以打开 v6 数据库：
v1.4.3 会以 `STATE_SCHEMA_TOO_NEW` fail-closed，必须先恢复 schema-v5 备份。

## 6. Event Experience 迁移规则

- accepted outline 只有在提供结构化 `event_seeds` 时才自动派生 EventSeed 与
  intended contract；
- 派生结果必须绑定源 `commit_id`、artifact ID、artifact version、artifact
  revision 与内容 SHA-256；
- active accepted outline、revision 或内容发生漂移时，旧 manifest 立即失效，并在
  创建 receipt 或发起远端调用前阻断；
- 升级前已有的 accepted 正文、章纲和大纲全部 grandfather：零回填 intended
  contract，零修改既有 canon、commit、event 与 replay hash；
- 旧 artifact 可建立 `grandfathered_observed_only` review，用于描述现有文本实际
  带来的体验；它不反推“当时已经存在”的 intended contract；
- 只有再次设计、续写或产生新的 artifact revision 时，才进入新的 EventSeed /
  contract 生命周期；
- ExperienceReview 成功或失败都与剧情生命周期隔离；失败只写入脱敏的
  `.plot-rag/experience-review-diagnostics.jsonl`；
- `visible_in_story_artifacts=false` 时，故事产物泄漏 EventSeed、合同 sentinel、
  schema 或 identity 字段会在 proposal / extraction job 创建前以
  `STORY_ARTIFACT_CONTROL_TERM_LEAKAGE` 阻断，保持零 proposal、零 job。

## 7. Legacy item 迁移规则

- 不从物品名称、题材常识或 `attributes_json` 推断功能；
- 不把 legacy inventory 复制为新的 accepted bootstrap event；
- 已有 accepted event、`inventory_state` 行、唯一性与持有人原样保留；
- 来源足以确认具体唯一对象时，只建立可重建的 legacy self-instance 投影；
- 仅有名称、持有人或数量时保持 `legacy_unmodeled / legacy_inventory_only`；
- `attributes_json` 只作为 legacy attributes 展示；
- 既有 PowerBinding 保持原样，不复制 AbilityDefinition；
- 只有新的逐字证据经过 proposal、grant、CAS 和 accept 后，才可晋升为强类型
  Definition、Instance、Function 或 Runtime 事实。

## 8. `plot-rag-delta/v4`

canonical 入口为：

```text
schemas/plot-rag-delta/v4.schema.json
```

v4 envelope 顶层只允许：

```json
{
  "schema_version": "plot-rag-delta/v4",
  "deltas": []
}
```

- 非物品事件继续使用冻结的 v3 candidate 字段和语义；
- item candidate 只携带 mention、动作、显式变化、故事坐标、知识平面、置信度
  与连续逐字证据；
- 稳定 ID 由本地 resolver 生成；
- before/after/current/remaining、资源扣减、冷却终点和守恒结果由本地 validator /
  reducer 计算；
- v3 envelope 与 legacy `inventory` 继续兼容读取。

## 9. 初始化兼容与 item sidecar

初始化协议不升为 v3：

- 通用项目继续使用 `plot-rag-init/v1`；
- 含结构化力量体系的项目继续使用 `plot-rag-init/v2`；
- 物品标准化结果固定写入：

```text
<PROJECT_ROOT>/.plot-rag/items.v1.json
```

其 canonical schema 入口为：

```text
schemas/plot-rag-item/v1.schema.json
```

sidecar 绑定原初始化 schema、source snapshot hash、claim provenance 与 package
hash。只有明确来源支持的定义、实例、功能、保管和运行态进入强类型数组；仅有名称
或持有人时继续保留 legacy inventory；单次效果证据继续保留为 observation。

## 10. 迁移后验证

```powershell
python -B -X utf8 .\scripts\plot_state.py doctor `
  --project-root "<PROJECT_ROOT>"

python -B -X utf8 .\scripts\plot_state.py replay `
  --project-root "<PROJECT_ROOT>"

python -B -X utf8 .\scripts\plot_state.py performance status `
  --project-root "<PROJECT_ROOT>"

python -B -X utf8 .\scripts\plot_state.py item inventory `
  --project-root "<PROJECT_ROOT>" `
  --mention "<ACTOR>"
```

至少核对：

1. canon revision 未因 schema 迁移增长；
2. accepted commit/event 数量不变；
3. legacy 角色、关系、位置、库存、故事时间和力量查询结果不变；
4. legacy continuity projection hash 不变；
5. `item_projection_hash` 可重复 replay；
6. event experience 与 extraction control rows 未进入正典查询；
7. config、日志、性能报告和错误信息没有凭据；
8. Prepare v1 仍是默认权威路径；
9. accepted outline 的 commit/artifact/revision/content-hash 任一漂移都会在零
   receipt、零远端调用处阻断；
10. grandfather artifact 只有 observed-only review，没有 intended contract 回填；
11. review revision 不推进 lifecycle binding revision，review 失败也不改变
    proposal、job 或 canon；
12. 故事产物内部控制术语泄漏保持零 proposal、零 extraction job；
13. `SessionEnd` 会把 queued、running、failed 与 pending-review 屏障持久化到
    `.plot-rag/session-close-pending.json`，accepted 与 no-delta 会清除对应记录；
14. worker、SessionEnd 与 review diagnostics 均遮罩 Bearer、Authorization、
    password、token、cookie、常见 key 前缀及敏感环境变量值。

迁移验证应在合成 fixture 或调用方自行准备的隔离副本中执行，并比较迁移前后 legacy query 与 projection hash。

## 11. 分阶段启用

建议顺序：

1. schema v6 + item query，只读，同时保持 Event Experience 默认硬门禁；
2. Prepare v2 shadow，对照 query、候选、selected chunks、context 与 critical fact；
3. async extraction shadow，对照 proposal；shadow proposal 不可 accept，也不进入 barrier；
4. item strict shadow，对照 reducer、守恒、可读 Markdown 和 replay；strict=false 时 v4 不可 accept；
5. 项目级验收通过后，分别切换 Prepare v2、async strict 与 item strict。

不要把一次成功迁移等同于严格路径已通过。真实作品仍需完成 new/ingest/hybrid、
Hook/CLI/MCP E2E、branch/historical/timeless/planned、correction/retraction/
supersession、Windows/Linux 与安装缓存一致性验收。

## 12. 回滚

1. 停止所有目标项目进程；
2. 保存失败后的数据库、日志与诊断报告供比对；
3. 校验 schema-v5 backup 的完整性与哈希；
4. 若要降级到 v1.4.3，先用备份恢复 `state.sqlite3`，不要让 v1.4.3 直接打开
   schema v6，也不要混用迁移后的 WAL/SHM；
5. 恢复旧 config，或在 v1.5 内关闭 Prepare v2、async extraction 与 item strict；
6. Event Experience 只切 shadow 时设置
   `event_experience.required_before_event_design=false`，完全关闭时设置
   `event_experience.enabled=false`；
7. 重新运行 doctor、legacy query 与 projection hash 核验；
8. 保留 accepted source 与 Git 项目文件原样。

回滚 v6 派生状态不应删除、改写或重新生成 accepted source、accepted commit 与
immutable continuity event。
