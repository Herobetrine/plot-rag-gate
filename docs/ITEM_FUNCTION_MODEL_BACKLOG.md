# 通用物品功能模型实施快照

## 状态

- 登记日期：2026-07-17
- 状态：主要 schema、迁移、reducer、初始化 sidecar 与查询面已实现；发行验证统一使用仓库内合成 fixture
- 兼容默认：`items.strict_runtime_validation=false`
- rollout：strict=false 只做 v4 reducer dry-run 与 warning diagnostics；authority
  accept 在 grant 消费前以 `ITEM_STRICT_RUNTIME_DISABLED` 阻断，legacy v3
  inventory 继续兼容
- 迁移指南：[../V1_5_MIGRATION.md](../V1_5_MIGRATION.md)

## 已形成的实现面

### Schema 与协议

- continuity schema：`6`
- item projection schema：`plot-rag-item-projection/v1`
- 初始化 sidecar：`plot-rag-item/v1`
- mixed extraction envelope：`plot-rag-delta/v4`
- canonical schema 入口：
  - `schemas/plot-rag-item/v1.schema.json`
  - `schemas/plot-rag-delta/v4.schema.json`

v4 envelope 顶层只允许 `schema_version + deltas`。非物品候选继续使用冻结的 v3
字段和语义；item 候选只提供 mention、动作、显式变化、故事坐标、知识平面、
置信度与逐字证据。本地 resolver 生成稳定 ID，本地 validator/reducer 计算
before/after、扣减、冷却终点和守恒结果。

### Continuity v6 表

- `item_definitions`
- `item_instances`
- `item_stacks`
- `item_function_definitions`
- `item_function_bindings`
- `item_custody_state`
- `item_runtime_state`
- `item_function_runtime_state`
- `item_use_history`
- `item_observations`
- `item_projection_meta`

旧 continuity projection hash 的定义冻结；上述新表由独立
`item_projection_hash` 覆盖，可单独 replay。

### 对象与事件

已建模：

- `ItemDefinition`：类别、stack policy、唯一性、容量、默认功能与稳定属性；
- `ItemInstance / ItemStack`：唯一实例与同质/批次库存；
- `ItemFunctionDefinition / Binding`：激活、前置、effect owner、成本、副作用与
  Ability bridge；
- `ItemCustodyState`：法律所有权、carrier、custodian、container、location 与
  access 分离；
- `ItemRuntimeState / ItemFunctionRuntimeState`：耐久、能量、次数、冷却、封印、
  损坏、激活、装备、绑定、解锁与抑制；
- `ItemUseHistory / ItemObservation`：accepted 使用记录与知识平面观察；
- item event family：`item_spec / item_instance / item_custody / item_runtime /
  item_use / item_observation / item_correction`。

### Legacy 兼容

- `inventory_state`、旧查询、旧事件与 canon revision 原样保留；
- `entity_type=item` 只生成来源可重建的 legacy definition；
- 来源支持唯一自身份时使用 `legacy_self_instance`；
- 其他库存保持 `legacy_unmodeled / legacy_inventory_only`；
- 不从名称或 `attributes_json` 猜功能，不复制 accepted bootstrap event；
- 既有 PowerBinding 保留，不复制 AbilityDefinition。

### 初始化

`InitializationBundle v1/v2` 不升版。物品标准化包固定写入：

```text
.plot-rag/items.v1.json
```

sidecar 绑定 source snapshot hash、claim provenance 与 package hash。只有显式来源
支持的功能、唯一性、保管和运行态进入强类型数组；单次效果证据保留为 observation。

### 查询面

CLI：

- `item definition / instance / inventory / custody`
- `item function / runtime / history / observations`

MCP：

- `query_item_definition`
- `query_item_instance`
- `query_item_function`
- `query_item_runtime`
- `query_item_custody`
- `query_actor_inventory`
- `query_item_history`
- `query_item_observations`

八个 MCP 物品查询均为 `readOnlyHint=true`。

### Mandatory context 与可读投影

- 战斗、解谜、逃生、生产、治疗、交易、权限和证据任务必须注入相关 definition、
  function、custody 与 runtime；
- `readable_projection=true` 时，accept/retract/replay 从同一 read snapshot
  原子刷新 `.plot-rag/物品/物品索引.md`、定义卡与 `实例/` 卡；
- 发布失败返回 degraded receipt，不回滚 accepted canon；
- `readable_projection=false` 时保留既有人工文件，不刷新投影树。

## 冻结不变量

- 功能定义与当前运行态分离；
- 物品身份与保管态分离，所有权转移和实物交接分别记录；
- 普通物品与力量物品共享核心对象，力量语义由可选绑定扩展；
- 远端模型只抽动作、对象、故事坐标与逐字证据，本地计算 before/after 和资源扣减；
- 物品功能使用时校验持有、位置、装备、充能、耐久、冷却和前置条件；
- 唯一物品的 custody、所有权、位置和销毁状态保持守恒；
- 未公开功能、角色误解和客观机制分别保存；
- 旧项目维持兼容，空字段保持合法。

## 待最终验收

- Hook、Stop、worker、proposal、grant、accept 与 item reducer 的整链 E2E；
- branch、historical、timeless、correction、retract、supersede 与 replay 矩阵；
- 容器嵌套无环、容量、stack split/merge 与销毁清理的全量回归；
- init new/ingest/hybrid materialize 与 sidecar target-drift 回归；
- 合成 fixture 迁移只产生来源支持内容；
- v5→v6 备份、恢复、legacy hash 不变与独立 item hash 复现；
- Windows/Linux、release gate、marketplace 重装与 source/cache/install 一致性。
