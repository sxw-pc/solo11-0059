# 设计说明

## 1. 目标与非目标

`scanamp-audit` 是一个离线静态审计器：不执行查询，只根据表布局、文件清单、sidecar 统计和查询谓词，估计一次查询可能触碰的分区/文件/字节，并解释裁剪失效原因。

目标：

- 对本地湖表目录做可重复、可解释的静态分析。
- 对分区裁剪和文件级统计裁剪分别建模。
- 对“确定可裁剪”和“只能保守保留”做严格区分。
- 产出可归档的 JSON/Markdown/HTML 证据和可幂等写入的 SQLite 台账。
- 让相同输入、配置和 run id 产生确定性的 summary，便于 golden 回归和 CI gate。

非目标：SQL 执行、查询计划、数据库连接、Parquet/ORC/Avro 解析、表维护、自动优化、时间旅行、schema 演进、抽样、血缘、调度、权限后台和外部 API。详见 README 的“是 / 不是”。

## 2. 模块结构

| 层 | 模块 | 职责 |
| --- | --- | --- |
| 入口 | `cli.py` | 子命令解析、默认 run id、输出目录、ledger、gate 退出码 |
| 配置 | `config.py` | 严格 YAML 校验、规则阈值、severity、默认选择率、配置指纹 |
| 安全 | `security.py` | 相对路径规范化、root 目录包含检查、loopback host、run id、ledger 路径 |
| 基础 | `schema.py` | 类型集合、字面量/统计值转换、ISO 日期、规范 JSON、SHA-256、浮点舍入 |
| 基础 | `errors.py` | 统一错误模型和可序列化的 location/field/reason/details |
| 模型 | `models.py` | 表、文件、分区、查询、配置、诊断、审计结果 |
| 加载 | `loader.py` | manifest 校验、路径推断、分区分组、访问统计、指纹 |
| 加载 | `workload.py` | workload 校验、谓词解析/类型校验、查询分析、指纹 |
| 表达式 | `expr/tokenizer.py` | 无 SQL 执行的位置感知词法分析 |
| 表达式 | `expr/parser.py` | 递归下降解析 WHERE 子集为 JSON 可序列化 AST |
| 表达式 | `expr/validate.py` | 模式类型检查、分区列/函数包裹/不可判定原因分析 |
| 表达式 | `expr/evaluator.py` | 三值逻辑、NNF、精确值/统计值求解 |
| 表达式 | `expr/stats.py` | min/max、null_count、distinct_count 的可能命中判断和叶子选择率 |
| 表达式 | `expr/intervals.py` | 闭区间相交、日期/数字可比较化、重叠比例 |
| 表达式 | `expr/estimate.py` | AND 乘积、OR 并集、叶子选择率的静态估计 |
| 引擎 | `engine/partition_pruner.py` | 用精确分区值裁剪分区，输出 keep/drop/unknown 决策 |
| 引擎 | `engine/file_pruner.py` | 用 sidecar 统计裁剪文件，输出扫描/丢弃/未知和估计命中 |
| 引擎 | `engine/metrics.py` | 裁剪比例、扫描字节、放大倍数、OR 分支扩张 |
| 引擎 | `engine/diagnostics.py` | 8 条可配置诊断规则 |
| 引擎 | `engine/auditor.py` | 编排查询结果、summary、诊断 ID、输入指纹 |
| 报告 | `report/format.py` | 格式化、Markdown/HTML 转义、JSON 稳定输出 |
| 报告 | `report/*_report.py` | JSON、Markdown、自包含 HTML |
| 报告 | `report/renderer.py` | 四件套写入和 artifact SHA-256 |
| 台账 | `ledger.py` | SQLite schema、事务内替换 run、只读查询 |
| 页面 | `server.py` | Flask 只读路由、GET/HEAD 限制、安全响应头、自动转义 |

所有模块只依赖 Python 标准库、PyYAML 和 Flask（仅 `server.py`/`cli.py` 的 serve 路径）。分析引擎不依赖 Flask，也不访问网络。

## 3. 数据流

```text
manifest.yaml ──> loader.load_table ──> TableManifest
                                           │
workload.yaml ──> workload.load_workload ─> Workload ──> parse/validate/analyze
                                           │
audit.yaml   ──> config.load_config ──────> AuditConfig
                                           │
                                           v
                                  engine.auditor.run_audit
                    ┌──────────────────────┼──────────────────────┐
                    v                      v                      v
             partition_pruner         file_pruner            metrics
                    │                      │                      │
                    └──────────────┬───────┴──────────────────────┘
                                   v
                            diagnostics rules
                                   │
                                   v
                       AuditResult (queries/summary)
                          │                    │
                          v                    v
                 report renderer         ledger.record_run
                 json/md/html/summary    SQLite archive
                                               │
                                               v
                                      server read-only view
```

每一步都是纯函数或显式持有少量状态的对象；审计引擎不修改 manifest、文件或表目录。

### 3.1 输入加载

1. `load_table` 读取 `_metadata/manifest.yaml`。
2. 校验表名、列类型、分区列、文件条目的字段和值。
3. 对每个 `files[].path` 做相对路径规范化，并用 `safe_join` 检查解析后仍位于 lake root。
4. 检查文件存在、是普通文件，且磁盘 `st_size` 与声明的 `byte_size` 一致。
5. 校验 `partition_values` 与分区列严格一致，做类型转换和 NULL 拒绝。
6. 校验 sidecar 统计：类型、`has_stats`、min/max 必填、min ≤ max、null/distinct 计数边界。
7. 从路径中的 Hive 目录推断分区值，和 manifest 声明值对比，记录 mismatch 证据。
8. 按分区值分组为 `Partition`，按路径排序文件；生成不含绝对 root 的规范指纹。

workload 解析后立即进行类型校验；查询按 id 排序。config 在合并默认值后重新序列化并计算指纹。

### 3.2 谓词处理

`expr/tokenizer.py` 生成带位置信息的 token；`expr/parser.py` 用递归下降构建 AST。AST 节点为普通字典，便于 JSON 报告、证据和 ledger 存储。

典型结构：

```json
{"node": "cmp", "op": ">", "left": {"kind": "column", "name": "amount"},
 "right": {"kind": "literal", "literal_type": "number", "value": 1000}}
```

布尔节点为 `and` / `or` / `not`；IN 为 `in`（含 `negated`），NULL 为 `is_null`，范围为 `between`。同层 AND/OR 会扁平化，`<>` 规范化为 `!=`，便于稳定比较。

`expr/validate.py` 完成：

- 未知列、比较两侧类型不匹配、非法日期字面量报错。
- 收集引用列和分区列。
- 识别函数包裹的分区列，记录 `FUNCTION_WRAPPED_PARTITION:<fn>(<col>)`。
- 识别分区列与数据列同时出现在一个叶子中的不可判定情况。

### 3.3 三值求值与 NNF

求值结果只有 `TRUE`、`FALSE`、`UNKNOWN`：

- 精确分区值或完整统计可以给出确定结果。
- 缺统计、函数包裹、无法确定的范围给出 `UNKNOWN`。
- 裁剪条件是 **结果为 FALSE**；`UNKNOWN` 必须保留候选，避免误删。

`to_nnf` 在求值和估计前把 `NOT` 推到叶子，使用 De Morgan 法则并翻转比较符。这样 `NOT (predicate)` 不会因为外层 NOT 而把本应保留的文件判成 FALSE。

AND 组合：任一 FALSE → FALSE；全部 TRUE → TRUE；否则 UNKNOWN。
OR 组合：任一 TRUE → TRUE；全部 FALSE → FALSE；否则 UNKNOWN。

### 3.4 分区级裁剪

每个分区的分区值已知，数据列未知。`partition_pruner` 构造：

```text
{partition_col: {"kind": "exact", "type": ..., "value": ...}}
```

数据列没有 resolver，因此求值会返回 UNKNOWN。只有分区谓词确定 FALSE 的分区才会被丢弃。决策原因用于报告：

- `NO_PREDICATE`
- `PARTITION_PREDICATE_TRUE` / `PARTITION_PREDICATE_FALSE`
- `NO_PARTITION_PREDICATE`
- `PARTITION_PREDICATE_UNKNOWN_WITH_DATA`
- `UNDECIDABLE_PARTITION_PREDICATE`

`selected_partitions` 是报告字段；内部 `selected_ids` 在审计编排中被移除，避免重复证据。

### 3.5 文件级统计裁剪

对通过分区裁剪的每个文件，resolver 包含：

- 分区值：精确值。
- 每个有统计的数据列：min/max/null_count/distinct_count/row_count。
- 无统计或 `has_stats=false`：`unknown`。

求值仍遵循“只有 FALSE 才丢弃”。`file_pruner` 同时统计：

- 扫描文件、字节、行。
- 丢弃文件、未知文件。
- 缺统计的过滤列和受影响文件数。
- 每个保留文件的估计选择率、估计命中行数和字节数。

`filtering_columns` 由 workload 分析提供；这保证 `STATS_MISSING` 只针对查询实际过滤的列，而不是所有列。

### 3.6 指标

扫描放大：

```text
scan_amplification = min(round(scanned_bytes / max(estimated_matched_bytes, 1)), 1_000_000)
```

裁剪比例：

```text
partition_prune_ratio = (table_files - after_partition_files) / table_files
stats_prune_ratio     = (after_partition_files - scanned_files) / after_partition_files
total_prune_ratio     = (table_files - scanned_files) / table_files
scan_byte_ratio       = scanned_bytes / table_bytes
```

OR 分支扩张只在顶层 AST 为 OR 时计算：分别对每个分支做文件裁剪，再对 OR 并集做一次，扩张因子为 `union_scanned_files / max(branch_scanned_files)`。这解释“每个分支都选择性不错，合并后却扫很多文件”的放大。

### 3.7 选择率估计

选择率只用于估计命中行/字节，不改变裁剪 keep/drop 决定。叶子估计使用：

- 等值：`1 / distinct_count`（有 distinct 统计时），否则默认选择率。
- 范围：min/max 与查询区间的重叠比例。
- IN/BETWEEN：按离散值或区间比例组合。
- IS NULL：`null_count / row_count`。
- 缺统计：配置的 `default_selectivity`。

AND 为子选择率乘积；OR 使用 `1 - ∏(1 - s_i)`，并钳制到 `[0, 1]`。日期通过 `date.toordinal()` 转为可比较数字计算重叠。

### 3.8 诊断与 summary

`engine/diagnostics.py` 在查询被裁剪和计量之后运行。每个规则读取配置的 `enabled`、`severity` 和阈值；未启用则完全跳过。诊断对象先按严重级别、规则 ID、查询 ID、证据 JSON 排序，然后按 `diag-0001` 顺序编号，保证相同输入得到相同 ID。

`auditor` 生成：

- 每条查询的完整决策。
- 每查询的诊断 ID/count。
- 汇总 counts、扫描指标、排名、诊断摘要。
- manifest/workload/config 指纹。
- 运行时 `generated_at`（summary 不含它）。

### 3.9 报告与台账

`renderer` 写四个文件并计算 SHA-256：

1. `report.json`：完整结构，稳定 key 顺序。
2. `report.md`：人读证据，表格和诊断证据代码块均转义。
3. `report.html`：内联 CSS、CSP meta、转义动态文本，无外部链接。
4. `summary.json`：去掉运行时字段的确定性摘要。

ledger 在一个事务中先删除相同 `run_id` 的子记录和主记录，再插入新记录；`runs.run_id` 为主键，子表使用外键并启用 `PRAGMA foreign_keys = ON`。因此同一个 run id 重跑不会产生重复数据。删除之前，`versions.archive_current` 会把被替换版本的逐查询裁剪明细（逐分区、逐文件 decision 与指标）归档为 `run_versions` 中的不可变快照：版本号按 run 单调递增，并记录写入时间；该表故意不设外键，快照不随 run 行替换而级联删除。`versions.py` 提供只读读模型：`list_versions` 列出版本历史，`get_version` 按版本号或别名（`current`/`latest`/`previous`）读取某一版本，`diff_versions` 仅从台账重建两版差异——逐查询的分区集合增减、扫描文件/字节变化、逐文件 decision 变化，以及仅估计值变化而裁剪决策未变的查询；输出确定性排序。

## 4. 确定性策略

确定性是验收条件之一。实现上：

- manifest 文件按路径排序，分区按规范化值排序，查询按 id 排序。
- 诊断按 severity/rule/query/evidence 排序后统一编号。
- JSON 使用 `sort_keys=True`、`ensure_ascii=False` 和固定缩进。
- 指纹使用规范 JSON（排序 key、统一浮点表示）的 SHA-256。
- 百分比和选择性统一 `round_float`（最多 6 位小数）。
- summary 不包含 `generated_at`；报告文件名、artifact 哈希只随内容变化。
- 不使用随机数、当前时间（除 `generated_at`）、进程/环境信息或绝对路径生成结果。
- golden 测试只比较 summary 和 diagnostics；`test_summary_is_deterministic` 显式使用不同运行时间验证除运行时字段外一致。

## 5. 安全与离线

- 路径：manifest 路径先规范化，再与 lake root 的解析结果做包含检查；拒绝绝对路径、盘符、`..`、`.`、空组件和 NUL。
- 输出/ledger 路径默认限制在工作目录内；显式 `--allow-ledger-outside-workdir` 才允许外部 ledger。
- host：只允许 `127.0.0.1`、`localhost`、`::1`，其他值在启动前拒绝。
- 页面：只允许 GET/HEAD；无写路由；Jinja 强制 autoescape；CSP 禁止外部资源。
- 报告：HTML 动态文本转义；Markdown 表格和代码内容转义；无 CDN、外部字体、图片或脚本。
- 网络：测试使用 socket monkeypatch 禁用连接，并断言 demo 在断网条件下仍可完成。
- fixture：全部为合成事件、合成 CSV 和 sidecar 统计，不含真实凭据或真实目标。

## 6. 错误模型

所有可预期错误继承 `ScanAmpError`，携带 `message`、`location`、`field`、`reason`、`details`，CLI 统一输出 `error: ...` 并返回 2。主要类别：

- `ConfigError`：配置结构、阈值、schema version、fail-on。
- `ManifestError`：文件、路径、尺寸、分区值、统计。
- `WorkloadError` / `PredicateError`：workload 结构、query id、语法、类型。
- `SecurityError`：非本地 host、run id、路径逃逸、ledger 路径。
- `LedgerError`：缺库、打开、schema/读写错误。
- `ReportError`：报告写入错误。

错误信息不包含凭据；manifest 绝对 root 不写入指纹或报告输入。

## 7. 测试与规模策略

测试按模块拆分，重点覆盖边界而不是只测 happy path：

- `test_expr_tokenizer.py` / `test_expr_parser.py`：词法、优先级、AST、非法语法、类型检查、函数分析。
- `test_intervals.py`：闭区间、点区间、日期重叠。
- `test_partition_pruner.py` / `test_file_pruner.py`：AND/OR/NOT、NULL、缺统计、保守保留。
- `test_metrics.py` / `test_diagnostics.py`：公式、OR 扩张、每条规则的命中/不命中和配置。
- `test_loader_security.py`：路径穿越、绝对路径、尺寸/分区/统计错误。
- `test_report_render.py`：转义、自包含、无外链。
- `test_ledger.py`：幂等、替换、哈希、只读。
- `test_ledger_versions.py`：快照保留与不可变、版本递增、差异重建（分区集合、扫描变化、仅估计值变化）、错误路径。
- `test_cli_integration.py`：命令行、输出、gate。
- `test_server_offline.py`：路由、只读、本地绑定、无非本地 host。
- `test_golden.py`：summary/diagnostic golden 和确定性。

代码规模按 `src` + `tests` 物理行统计：4922 行（源码 4023、测试 899）；版本快照与差异重建（`versions.py`）使总量超出最初的 4500 行上限。单文件最大 288 行，低于 300 行上限。为控制行数，模块内不强制保留函数之间的空行，但通过 docstring、分段顺序和独立模块维持可读性。

## 8. 扩展点

- 新数据类型：扩展 `schema.SUPPORTED_TYPES`、`coerce_value` 和对应统计比较语义，并补测试。
- 新谓词：在 tokenizer/parser 增加语法，在 validate/evaluator/estimate/stats 增加三值语义，禁止只改 parser。
- 新诊断：在 `RULE_DEFAULTS` 增加默认值和阈值，在 `diagnostics.py` 增加纯函数规则，并加命中/不命中测试。
- 新报告：保持四个标准产物不变，新增产物应在 `renderer` 中登记并写 artifact 哈希。
- 新数据源：只能扩展为同一 manifest 抽象，不引入运行时数据库连接或真实 Parquet 解析。
