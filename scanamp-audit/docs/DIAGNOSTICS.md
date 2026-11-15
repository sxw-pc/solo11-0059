# 诊断规则手册

本文件描述 `scanamp-audit` 的 8 条静态诊断规则。诊断只解释扫描放大和裁剪失效，不会自动修改表、分区、统计或查询。

## 通用行为

- 每条规则都可以在 audit config 中通过 `enabled: false` 关闭，或覆盖 `severity` 和 `thresholds`。
- 未在配置中出现的规则使用 `src/scanamp_audit/config.py` 中的默认值。
- 规则在查询级裁剪和指标计算之后运行；`PATH_MANIFEST_MISMATCH` 是表级规则，`query_id=null`。
- 命中的诊断先按 `severity`（critical → warning → info）、`rule_id`、`query_id`、证据 JSON 排序，再编号为 `diag-0001`、`diag-0002`……
- 每条诊断包含 `message`、`query_id`、`evidence` 和 `suggestion`。证据字段进入 JSON、Markdown、HTML 和 SQLite。
- 诊断不是“自动优化建议”的执行器。建议只说明可能的治理方向，是否修改表由人决定。
- `--fail-on critical|warning|info` 使用严重级别阈值做 CI gate；`none` 不阻断。

严重级别语义：

| 级别 | 含义 |
| --- | --- |
| `critical` | 静态语义直接失效或路径/清单不一致，结论必须优先处理 |
| `warning` | 可裁剪性明显受损，通常导致扫描放大 |
| `info` | 布局或访问模式提示，不一定错误 |

## 1. `PARTITION_KEY_UNFILTERED`

- **默认 severity**：`warning`
- **阈值**：无
- **触发条件**：查询谓词没有引用任何分区列，或查询没有谓词。
- **证据**：
  - `partition_columns`：表的分区列。
  - `referenced_columns`：查询引用的全部列。
  - `where`：原始谓词。
- **含义**：所有分区都是候选，分区裁剪比例为 0；后续只能依赖文件统计裁剪。数据列过滤再精确，也无法利用分区布局。
- **建议**：在分区列上增加裸列谓词（例如 `dt` 或 `region` 的等值/范围过滤），再评估文件统计裁剪。
- **demo**：`q02_data_only_event`（只过滤 `event_type`）、`q07_null_status`（只做 `status IS NULL`）。

## 2. `LOW_SELECTIVITY_PARTITION`

- **默认 severity**：`warning`
- **默认阈值**：
  - `min_partition_prune_ratio: 0.5`
  - `min_scanned_file_ratio: 0.5`
  - `min_scanned_files: 2`
- **触发条件**：同时满足：
  - `partition_prune_ratio >= min_partition_prune_ratio`；
  - `scanned_files / table_files >= min_scanned_file_ratio`；
  - `scanned_files >= min_scanned_files`。
- **证据**：`partition_prune_ratio`、`scanned_file_ratio`、`scanned_files`、`table_files`。
- **含义**：分区裁剪已经生效，但选中的分区里仍有大量文件无法被统计裁剪；放大来自分区内文件级统计缺失或选择性不足，而不是分区选择失效。
- **建议**：补齐过滤列的 min/max 统计、提高统计质量，或按过滤键调整分区/分桶粒度。
- **demo**：`q05_missing_user_stats`：`region = 'alpha'` 只保留 alpha 分区，但 `user_id` 缺统计，所有候选文件都被保留。

## 3. `HIGH_CARDINALITY_OVERPARTITION`

- **默认 severity**：`info`
- **默认阈值**：
  - `min_partition_count: 4`
  - `min_selected_partition_ratio: 0.8`
  - `min_selected_partitions: 2`
- **触发条件**：同时满足：
  - `partition_count >= min_partition_count`；
  - `selected_partitions / partition_count >= min_selected_partition_ratio`；
  - `selected_partitions >= min_selected_partitions`。
- **证据**：`partition_count`、`selected_partitions`、`selected_ratio`、`partition_columns`。
- **含义**：分区数不少，但查询仍然选中了 80% 以上的分区。可能是分区粒度过细、分区键与查询模式不匹配，或查询范围过宽。
- **建议**：评估分区键粒度和查询访问模式；考虑更粗的分区粒度或对高基数过滤键使用分桶。
- **demo**：`q02_data_only_event`、`q03_function_dt`、`q06_wide_date_range`、`q07_null_status`。

## 4. `FUNCTION_WRAPPED_PARTITION`

- **默认 severity**：`critical`
- **阈值**：无
- **触发条件**：谓词的函数参数中出现分区列。例如：
  - `substr(dt, 1, 7) = '2024-01'`
  - `lower(region) = 'alpha'`
- **证据**：
  - `functions`：`[{"column": "dt", "function": "substr"}]`。
  - `where`：原始谓词。
  - `partition_columns`：表的分区列。
- **含义**：函数调用不能转换为分区值上的静态比较，分区裁剪无法确定，所有分区保守保留；同时会产生 `UNKNOWN_PREDICATE_CONSERVATIVE`。
- **建议**：把函数从分区列上移走，直接比较裸分区列；或者把派生值物化为可过滤列/分区值。
- **demo**：`q03_function_dt`。

## 5. `STATS_MISSING`

- **默认 severity**：`warning`
- **默认阈值**：`min_missing_filter_columns: 1`
- **触发条件**：候选文件中，至少 `min_missing_filter_columns` 个查询实际过滤的数据列缺少可用统计；同一查询的缺列集合非空。
- **统计可用性**：文件没有该列的 `columns` 条目，或 `has_stats: false`，或 min/max 不可用。
- **证据**：
  - `columns`：缺统计的过滤列（排序后）。
  - `affected_files`：受影响文件数。
  - `scanned_files`：该查询最终扫描文件数。
- **含义**：统计裁剪无法为该列计算 min/max 交集，只能保守保留文件。行/字节命中仍按默认选择率估计，因此扫描放大可能被低估或高估。
- **建议**：为过滤列采集 sidecar min/max 统计并保持新鲜；如果无法采集，调整数据布局或查询。
- **demo**：`q05_missing_user_stats`（`user_id` 缺统计）、`q08_in_and_between`（alpha 分区文件缺 `user_id` 统计）。

## 6. `PATH_MANIFEST_MISMATCH`

- **默认 severity**：`critical`
- **阈值**：无
- **触发条件**：文件路径中的 Hive 风格目录推断出的分区值，与 manifest 的 `partition_values` 不一致。每个不一致文件产生一条诊断。
- **证据**：
  - `file`：规范化相对路径。
  - `column`：分区列名。
  - `inferred`：从路径推断的值（已规范化）。
  - `declared`：manifest 声明的值。
  - `reason: "PATH_MANIFEST_MISMATCH"`。
- **含义**：表的物理路径语义与 manifest 声明冲突。基于 manifest 的分区裁剪和基于目录的读取可能指向不同数据；这是数据治理问题，不只是性能问题。
- **建议**：修正目录命名或 manifest 分区值，重新生成清单并复审计。
- **demo**：`dt=2024-01-01/region=beta/part-0001.csv` 声明为 `region: delta`，路径推断为 `beta`。该诊断的 `query_id=null`。

## 7. `OR_BRANCH_BLOWUP`

- **默认 severity**：`warning`
- **默认阈值**：
  - `min_branches: 2`
  - `min_expansion_factor: 1.5`
- **覆盖集合**：每个分支的候选覆盖集合按文件身份（`partition_id` + `path`）构造，语义与 `prune_files` 的 keep/drop/unknown 一致：`keep` 与 `unknown` 都算覆盖，只有 `drop` 不覆盖。被两个及以上分支覆盖的文件是共享文件，只被一个分支覆盖的是独占文件。
- **触发条件**：谓词顶层节点为 `or`，分支数至少达到 `min_branches`，且：
  - `expansion_factor = union_scanned_files / max(branch_scanned_files) >= min_expansion_factor`；
  - `expansion_delta = union_files - max(branch_files) >= 1`，即并集确实比最大单分支多扫文件。等价覆盖（各分支在同一批文件上都 UNKNOWN，并集与单分支相同）即使把阈值调到 `1.0` 也不会触发。
- **证据**：
  - `branch_count`：顶层 OR 分支数。
  - `branch_scanned_files`：各分支扫描文件数列表。
  - `union_scanned_files`：并集扫描文件数。
  - `expansion_factor`：扩张因子。
  - `expansion_delta`：并集相对最大单分支多扫的文件数。
  - `shared_files` / `exclusive_files`：并集中被多分支共享 / 仅被单分支覆盖的文件数。
  - `shared_contribution` / `exclusive_contribution`：`expansion_delta` 的分解——最大分支漏掉的文件中，仍被多分支共享的数量与仅被单分支独占的数量，两者之和等于 `expansion_delta`。
  - `dominant_cause`：膨胀归因。`exclusive` 表示膨胀主要来自独占文件（拆分支有收益）；`shared` 表示主要来自共享文件（拆分支会重复扫描共享文件，降不了成本）。
  - `stats_missing_branches`：覆盖集合完全由 UNKNOWN 决策构成的分支下标（典型原因是统计缺失）。
- **含义**：每个分支单独看可能选择性不错，但 OR 的并集覆盖了更多文件。归因区分两种情形：各分支落在不同文件上（`exclusive`，拆开跑更划算），或多个分支共享同一批文件（`shared`，拆分支无法降成本）。当并集与最大分支相同、且最大分支全靠 UNKNOWN 保留文件时，扩张为 1.0，归因为统计缺失而不是 OR 拆分收益，不触发本规则。
- **建议**：`exclusive` 时按分支拆分查询并分别读取，或改写为并集过滤；`shared` 时拆分支会重复扫描共享文件，应改写为并集过滤或调整布局消除分支重叠。
- **注意**：分支指标基于全部表文件计算，而查询级指标基于分区裁剪后的候选文件；两处口径在报告中分别呈现，不能直接相除解释。每条 OR 查询的完整归因同时出现在 summary 的 `or_attribution` 中（按 `expansion_delta` 降序、查询 id 稳定排序），包括未触发诊断的查询。
- **demo**：`q04_or_branches`：两个分支选择不同分区，扩张因子约 `2.0`，膨胀全部来自独占文件。

## 8. `UNKNOWN_PREDICATE_CONSERVATIVE`

- **默认 severity**：`warning`
- **阈值**：无
- **触发条件**：所有分区都被保留（`all_selected=true`），并且分析产生了至少一个不可判定原因。典型原因：
  - 分区列被函数包裹。
  - 分区列与数据列在同一叶子谓词中混用。
- **证据**：
  - `reasons`：不可判定原因列表。
  - `selected_partitions` / `total_partitions`。
  - `where`：原始谓词。
- **含义**：由于静态无法判定，工具选择保守保留所有分区。这不是误报的“最坏情况猜测”，而是安全侧的正确行为；但会掩盖真实的扫描放大。
- **建议**：把不可判定谓词改写为分区列上的裸比较，或为派生值建立可静态过滤的列/分区。
- **demo**：`q03_function_dt`。

## 规则与 demo 对照

| 规则 | demo 命中查询 | 证据重点 |
| --- | --- | --- |
| `PARTITION_KEY_UNFILTERED` | `q02_data_only_event`、`q07_null_status` | 无分区列引用 |
| `LOW_SELECTIVITY_PARTITION` | `q05_missing_user_stats` | 分区比例 0.5，扫描文件 3/6 |
| `HIGH_CARDINALITY_OVERPARTITION` | `q02`、`q03`、`q06`、`q07` | 5 个分区选中大部分 |
| `FUNCTION_WRAPPED_PARTITION` | `q03_function_dt` | `substr(dt)` |
| `STATS_MISSING` | `q05`、`q08` | `user_id` 无统计 |
| `PATH_MANIFEST_MISMATCH` | 表级 | path `beta` vs declared `delta` |
| `OR_BRANCH_BLOWUP` | `q04_or_branches` | 并集相对最大分支的膨胀按共享/独占归因 |
| `UNKNOWN_PREDICATE_CONSERVATIVE` | `q03_function_dt` | 全分区保留 + 不可判定原因 |

## 配置示例

```yaml
diagnostics:
  LOW_SELECTIVITY_PARTITION:
    enabled: true
    severity: warning
    thresholds:
      min_partition_prune_ratio: 0.6
      min_scanned_file_ratio: 0.4
      min_scanned_files: 3
  OR_BRANCH_BLOWUP:
    enabled: false
```

关闭规则只影响诊断输出，不改变裁剪、扫描指标或 summary 中的扫描数字。若要改变估计行为，应调整数据统计或 `estimation.default_selectivity`，而不是关闭诊断。
