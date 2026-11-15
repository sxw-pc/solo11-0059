# 数据与接口契约

本文件定义 manifest、workload、audit config、报告 JSON 和 SQLite ledger 的结构。所有路径使用 POSIX 风格；运行在 Windows 时 manifest 内的 `/` 分隔符也会被正确解析为主机路径。

## 1. 通用类型

支持的列类型：

| 类型 | 规范值 | 说明 |
| --- | --- | --- |
| `string` | Python `str` | 统计比较使用字典序 |
| `integer` | Python `int`（非 bool） | manifest/统计中允许数字字符串；查询字面量必须是整数字面量 |
| `float` | Python `float` | manifest/统计中允许数字字符串；查询字面量允许整数或浮点数 |
| `date` | `YYYY-MM-DD` 字符串 | 必须是可被 `date.fromisoformat` 解析的 ISO 日期，按字典序排序 |

规则：

- 布尔值不被当作整数或数字。
- 查询字面量类型必须与列类型匹配；`NULL` 只能用于 `IS [NOT] NULL`。
- 所有统计值、分区值、查询 AST 和 JSON 报告都可 JSON 序列化。
- 浮点指标最多保留 6 位小数。

## 2. manifest.yaml

位置固定为 `<lake-root>/_metadata/manifest.yaml`。

```yaml
table: demo_events
description: Synthetic event table for the scanamp-audit demo
partition_columns:
  - name: dt
    type: date
  - name: region
    type: string
columns:
  - name: user_id
    type: integer
  - name: amount
    type: float
files:
  - path: dt=2024-01-01/region=alpha/part-0001.csv
    partition_values:
      dt: "2024-01-01"
      region: alpha
    row_count: 120
    byte_size: 94
    columns:
      amount:
        type: float
        has_stats: true
        min: 10.0
        max: 5000.0
        null_count: 0
        distinct_count: 3
```

### 2.1 顶层字段

| 字段 | 必填 | 类型 | 约束 |
| --- | --- | --- | --- |
| `table` | 是 | string | 非空 |
| `description` | 否 | string | 默认空字符串 |
| `partition_columns` | 否 | list | 每项 `{name, type}`；name 非空且唯一；type 属于支持类型 |
| `columns` | 否 | list | 每项 `{name, type}`；不得与分区列重名 |
| `files` | 是 | list | 文件条目的列表 |

`partition_columns` 为空表示非分区表。manifest 的加载校验聚焦于上述字段、文件条目和统计，不要求关闭 YAML 扩展；但只使用 `yaml.safe_load`，不会构造任意对象。

### 2.2 文件条目

| 字段 | 必填 | 类型 | 约束 |
| --- | --- | --- | --- |
| `path` | 是 | string | 表根目录内的相对 POSIX 路径 |
| `partition_values` | 是 | mapping | key 必须与声明的分区列完全一致；值不得为 NULL；按列类型转换 |
| `row_count` | 是 | integer | ≥ 0 |
| `byte_size` | 是 | integer | ≥ 0，且必须等于磁盘文件实际字节数 |
| `columns` | 否 | mapping | 列名 → sidecar 列统计；未知列报错；缺省表示无统计 |

路径安全：

- 拒绝绝对路径、Windows 盘符、`..`、`.` 和 NUL 字节。
- 反斜杠会规范化为 `/`。
- 规范路径经 `safe_join` 解析后必须仍位于 lake root；越界时报 `SecurityError`，加载器包装为 `ManifestError`，并保留 reason 和 path 证据。
- 同一路径在 manifest 中只能出现一次。
- 文件必须存在、是普通文件，且实际大小与 `byte_size` 完全相等。

路径推断：

- 对路径的父目录中形如 `key=value` 的片段进行识别。
- `key` 是声明的分区列时才推断。
- 推断值与 manifest 的 `partition_values` 按列类型规范化后比较。
- 不一致不会阻止加载；会写入 `path_mismatches`，并产生 `PATH_MANIFEST_MISMATCH` 诊断（`query_id=null`）。
- 每个 mismatch 证据包含 `file`、`column`、`inferred`、`declared`、`reason`。

### 2.3 sidecar 列统计

```yaml
columns:
  amount:
    type: float          # 可省略，默认取列声明类型；出现时必须一致
    has_stats: true       # 默认 true
    min: 10.0
    max: 5000.0
    null_count: 0
    distinct_count: 3
```

| 字段 | 必填 | 约束 |
| --- | --- | --- |
| `type` | 否 | 必须等于列声明类型 |
| `has_stats` | 否 | bool；默认 `true` |
| `min` / `max` | `has_stats=true` 时必填 | 按列类型转换；min ≤ max |
| `null_count` | 否 | 非负整数且 ≤ `row_count` |
| `distinct_count` | 否 | 非负整数且 ≤ `row_count` |

`has_stats: false` 或未提供某列的统计时，文件级裁剪将该列视为 `unknown`，不会据此丢弃文件。允许统计中缺少数据列；查询过滤这些列时会触发 `STATS_MISSING` 并保守保留。

### 2.4 指纹

manifest 指纹是以下规范 JSON 的 SHA-256：

```json
{
  "table": "...",
  "description": "...",
  "partition_columns": [{"name": "...", "type": "..."}],
  "columns": [{"name": "...", "type": "..."}],
  "files": [
    {
      "path": "...",
      "partition_values": {},
      "row_count": 0,
      "byte_size": 0,
      "columns": {}
    }
  ],
  "path_mismatches": []
}
```

不包含 lake root 绝对路径、文件系统 inode、修改时间或运行时间。相同内容的表放在不同目录下得到相同 manifest 指纹。

## 3. workload.yaml

```yaml
schema_version: "1.0"
description: Demo workload
queries:
  - id: q01
    description: Partition and data filter
    where: "dt = '2024-01-01' AND amount > 1000"
```

顶层字段：

| 字段 | 必填 | 约束 |
| --- | --- | --- |
| `schema_version` | 否 | 保留字段；当前实现不校验其值，也不做版本分派 |
| `description` | 否 | string |
| `queries` | 是 | 非空 list，或直接把顶层写成非空 list |

每个查询：

| 字段 | 必填 | 约束 |
| --- | --- | --- |
| `id` | 是 | `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`，全文件唯一 |
| `description` | 否 | string |
| `where` | 二选一 | 谓词字符串 |
| `predicate` | 二选一 | 与 `where` 等价的别名；两者同时出现时必须完全相同 |

查询按 `id` 排序后保存。workload 指纹只覆盖 `id`、`description`、`where`。

### 3.1 谓词语法

```text
expr        := or_expr
or_expr     := and_expr (OR and_expr)*
and_expr    := not_expr (AND not_expr)*
not_expr    := NOT not_expr | primary
primary     := '(' expr ')' | comparison
comparison  := operand (cmp_op operand | IN '(' literal (',' literal)* ')'
                       | NOT IN '(' literal (',' literal)* ')'
                       | BETWEEN literal AND literal
                       | NOT BETWEEN literal AND literal
                       | IS NULL | IS NOT NULL)
operand     := column | literal | function_call
function_call := identifier '(' operand (',' operand)* ')'
```

优先级：`NOT` > `AND` > `OR`。`<>` 规范化为 `!=`。同层 AND/OR 会扁平化。支持一元负号数字、单双引号字符串（连续引号表示转义）。函数调用只做静态分析，不求值。

### 3.2 AST

AST 节点示例：

```json
{"node": "and", "children": [
  {"node": "cmp", "op": ">", "left": {"kind": "column", "name": "amount"},
   "right": {"kind": "literal", "literal_type": "integer", "value": 1000}},
  {"node": "is_null", "negated": false,
   "operand": {"kind": "column", "name": "status"}}
]}
```

节点类型：

| node | 主要字段 |
| --- | --- |
| `and` / `or` | `children` |
| `not` | `child` |
| `cmp` | `op`、`left`、`right` |
| `in` | `operand`、`values`、`negated` |
| `is_null` | `operand`、`negated` |
| `between` | `operand`、`low`、`high`、`negated` |

操作数 `kind` 为 `column`、`literal` 或 `function`。函数节点额外有 `name`、`args`。所有节点 JSON 可序列化，便于直接进入报告和 ledger 证据。

### 3.3 类型与不可判定分析

加载 workload 时进行：

- 未知列检查。
- 列与字面量类型检查（日期必须是 ISO 字符串，整数不能用浮点字面量，float 接受整数/浮点）。
- 列与列比较的类型一致性。
- `IS NULL` 操作数必须是列或函数，不能是字面量。

静态分析输出：

- `columns`：引用的全部列。
- `partition_columns`：其中属于分区列的列。
- `function_wrapped_partition_columns`：`[{"column": "...", "function": "..."}]`。
- `undecidable_reasons`：稳定的原因字符串，例如：
  - `FUNCTION_WRAPPED_PARTITION:substr(dt)`
  - `PARTITION_COLUMN_COMPARED_WITH_DATA:region:user_id`

这些字段会进入报告和诊断证据。

## 4. audit config YAML

```yaml
schema_version: "1.0"
estimation:
  default_selectivity: 0.5
report:
  top_n: 10
diagnostics:
  FUNCTION_WRAPPED_PARTITION:
    enabled: true
    severity: critical
  STATS_MISSING:
    enabled: true
    severity: warning
    thresholds:
      min_missing_filter_columns: 1
```

顶层只允许 `schema_version`、`diagnostics`、`estimation`、`report`。未知顶层键、未知规则 ID、未知规则键和未知阈值键都会报 `ConfigError`。

| 字段 | 默认 | 约束 |
| --- | --- | --- |
| `schema_version` | `"1.0"` | 必须是 `"1.0"` |
| `estimation.default_selectivity` | `0.5` | 0–1 |
| `report.top_n` | `10` | 正整数；用于 summary 的放大排名长度 |
| `diagnostics.<rule>` | 规则默认值 | mapping，只允许 `enabled`、`severity`、`thresholds` |
| `enabled` | `true` | bool |
| `severity` | 规则默认 | `info` / `warning` / `critical` |
| `thresholds` | 规则默认 | 非负数；key 含 `ratio` 时必须在 0–1 |

8 条规则的默认 severity 和阈值见 [docs/DIAGNOSTICS.md](DIAGNOSTICS.md)。

配置指纹来自完整规范化后的配置（包含所有规则的默认项、阈值排序、`default_selectivity` 和 `top_n`）。仅改变 YAML 中的注释、键顺序或省略默认项不影响指纹。

## 5. 报告 JSON

`report.json` 顶层：

```json
{
  "schema_version": "1.0",
  "run_id": "demo-run",
  "generated_at": "2030-01-01T00:00:00Z",
  "inputs": {},
  "config": {},
  "table": {},
  "queries": [],
  "diagnostics": [],
  "summary": {}
}
```

### 5.1 `inputs`

| 字段 | 含义 |
| --- | --- |
| `lake_name` | lake root 的目录名，不含绝对路径 |
| `workload_file` | workload 文件名 |
| `config_file` | config 文件名 |
| `fingerprints` | `manifest` / `workload` / `config` 的 SHA-256 |

### 5.2 `table`

| 字段 | 含义 |
| --- | --- |
| `name` / `description` | manifest 声明 |
| `partition_columns` / `columns` | `[{name, type}]` |
| `files` | 每个文件的 path、partition_values、row_count、byte_size、columns 统计 |
| `partitions` | `partition_id`、values、file_count、row_count、byte_size |
| `totals` | file_count、row_count、byte_size、partition_count |
| `path_mismatches` | 路径推断与 manifest 不一致的证据 |

### 5.3 `queries[]`

每条查询包含：

| 字段 | 含义 |
| --- | --- |
| `id` / `description` / `where` | workload 信息 |
| `predicate` | AST；无谓词时为 `null` |
| `columns_referenced` | 引用列 |
| `partition_columns_referenced` | 引用的分区列 |
| `function_wrapped_partition_columns` | 函数包裹的分区列 |
| `partition` | 分区裁剪结果 |
| `files` | 文件裁剪结果 |
| `metrics` | 指标 |
| `or_branches` | 顶层 OR 分支分析；非顶层 OR 时为 `null` |
| `diagnostic_ids` / `diagnostic_count` | 该查询关联的诊断 |

`partition` 字段：

| 字段 | 含义 |
| --- | --- |
| `partition_count` / `selected_count` / `pruned_count` / `unknown_count` | 分区计数 |
| `all_selected` | 是否所有分区都保留 |
| `prune_ratio` | 被裁掉的分区比例 |
| `selected_partitions` / `pruned_partitions` | 分区摘要 |
| `decisions` | 每个分区的 `{partition_id, values, decision, reason}` |
| `undecidable_reasons` | 不可判定原因 |

`decision` 为 `keep` / `drop` / `unknown`。只有 `drop` 才会从后续文件裁剪中排除。

`files` 字段：

| 字段 | 含义 |
| --- | --- |
| `file_count` | 进入文件裁剪的候选文件数 |
| `decisions` | 每个文件的决策、原因、缺统计列、估计选择率/行/字节 |
| `scanned_files` / `scanned_bytes` / `scanned_rows` | 保留扫描集合 |
| `dropped_files` / `unknown_files` | 丢弃数 / UNKNOWN 保留数 |
| `estimated_matched_rows` / `estimated_matched_bytes` | 估计命中 |
| `stats_missing_columns` | 缺统计的过滤列 |
| `stats_missing_file_count` | 受影响文件数 |

文件 `decision` 为 `keep` / `drop` / `unknown`。`reason` 为 `PREDICATE_TRUE` / `PREDICATE_FALSE` / `PREDICATE_UNKNOWN`。

`metrics` 字段：

- `table_files` / `table_bytes` / `table_rows`
- `after_partition_files` / `after_partition_bytes` / `after_partition_rows`
- `scanned_files` / `scanned_bytes` / `scanned_rows`
- `estimated_matched_rows` / `estimated_matched_bytes`
- `partition_prune_ratio` / `stats_prune_ratio` / `total_prune_ratio`
- `scan_byte_ratio`
- `scan_amplification`

`or_branches` 非空时包含：`branch_count`、`branches[]`（index、scanned_files/bytes/rows、expression AST）、`union_scanned_files`、`union_scanned_bytes`、`expansion_factor`。

### 5.4 `diagnostics[]`

```json
{
  "id": "diag-0001",
  "rule_id": "FUNCTION_WRAPPED_PARTITION",
  "severity": "critical",
  "message": "...",
  "query_id": "q03_function_dt",
  "evidence": {},
  "suggestion": "..."
}
```

`query_id` 可以为 `null`（表级问题，例如路径/manifest 不一致）。诊断 ID 在排序后按 `diag-0001` 递增分配，不依赖输入 YAML 中的出现顺序。

### 5.5 `summary`

`summary.json` 只包含确定性字段，不含 `generated_at`：

```json
{
  "schema_version": "1.0",
  "run_id": "demo-run",
  "table": {
    "name": "...",
    "file_count": 6,
    "row_count": 570,
    "byte_size": 497,
    "partition_count": 5,
    "partition_columns": ["dt", "region"]
  },
  "fingerprints": {"manifest": "...", "workload": "...", "config": "..."},
  "counts": {
    "queries": 8,
    "diagnostics": 13,
    "critical": 2,
    "warning": 7,
    "info": 4
  },
  "scan": {
    "table_files": 6,
    "table_bytes": 497,
    "table_rows": 570,
    "scanned_files": 28,
    "scanned_bytes": 2370,
    "estimated_matched_bytes": 1280,
    "scan_amplification": 1.851562
  },
  "queries": [
    {
      "id": "q01",
      "where": "...",
      "scanned_files": 1,
      "scanned_bytes": 94,
      "scanned_rows": 120,
      "estimated_matched_rows": 96,
      "estimated_matched_bytes": 75,
      "scan_amplification": 1.253333,
      "partition_prune_ratio": 0.5,
      "stats_prune_ratio": 0.666667,
      "selected_partitions": 2,
      "diagnostic_count": 0
    }
  ],
  "ranking": [{"id": "q07", "scan_amplification": 64.75}],
  "diagnostics": [
    {"id": "diag-0001", "rule_id": "...", "severity": "critical", "query_id": "..."}
  ],
  "layout_evaluation": {
    "schema_version": "1.0",
    "table": {
      "name": "demo_events",
      "partition_columns": ["dt", "region"],
      "partition_count": 5,
      "file_count": 6,
      "byte_size": 497,
      "single_file_partition_count": 4,
      "single_file_partition_ratio": 0.8
    },
    "columns": [
      {
        "column": "dt",
        "type": "date",
        "cardinality": 3,
        "partition_count": 5,
        "values": [
          {"value": "2024-01-01", "partition_count": 2, "file_count": 3,
           "byte_size": 255, "row_count": 300, "single_file": false}
        ],
        "file_count_distribution": {"count": 3, "min": 1, "max": 3,
                                    "total": 6, "mean": 2.0, "median": 2.0},
        "byte_size_distribution": {"count": 3, "min": 78, "max": 255,
                                   "total": 497, "mean": 165.666667,
                                   "median": 164.0},
        "single_file_partition_count": 1,
        "single_file_partition_ratio": 0.333333
      }
    ],
    "layouts": [
      {
        "layout_id": "current",
        "removed_columns": [],
        "remaining_columns": ["dt", "region"],
        "partition_count": 5,
        "file_count": 6,
        "byte_size": 497,
        "single_file_partition_count": 4,
        "single_file_partition_ratio": 0.8,
        "merged_group_count": 0,
        "queries": [
          {
            "query_id": "q05_missing_user_stats",
            "referenced_partition_columns": ["region"],
            "retained_partition_filter_columns": ["region"],
            "removed_partition_filter_columns": [],
            "partition_count": 5,
            "selected_partition_count": 2,
            "unknown_partition_count": 0,
            "partition_prune_ratio": 0.6,
            "candidate_files": 3,
            "candidate_bytes": 254,
            "candidate_rows": 290,
            "candidate_file_prune_ratio": 0.5,
            "conservative_all_partitions": false,
            "partition_prune_ratio_delta": 0.0,
            "candidate_file_prune_ratio_delta": 0.0,
            "candidate_files_delta": 0,
            "candidate_bytes_delta": 0,
            "reasons": []
          }
        ],
        "totals": {"candidate_files": 37, "candidate_bytes": 3092}
      },
      {
        "layout_id": "without:region",
        "removed_columns": ["region"],
        "remaining_columns": ["dt"],
        "partition_count": 3,
        "merged_group_count": 2,
        "queries": [
          {
            "query_id": "q05_missing_user_stats",
            "removed_partition_filter_columns": ["region"],
            "retained_partition_filter_columns": [],
            "partition_count": 3,
            "selected_partition_count": 3,
            "partition_prune_ratio": 0.0,
            "candidate_file_prune_ratio": 0.0,
            "candidate_files": 6,
            "candidate_bytes": 497,
            "conservative_all_partitions": true,
            "candidate_files_delta": 3,
            "candidate_bytes_delta": 243,
            "reasons": [
              "CONSERVATIVE_ALL_PARTITIONS_RETAINED:region",
              "MERGED_PARTITION_PREDICATE_UNDECIDABLE:region",
              "PARTITIONS_MERGED:5->3",
              "QUERY_LOST_ONLY_PARTITION_FILTER:region",
              "REMOVED_PARTITION_FILTER_COLUMNS:region"
            ]
          }
        ],
        "totals": {"candidate_files": 45, "candidate_bytes": 3734,
                   "candidate_files_delta": 8, "candidate_bytes_delta": 642}
      }
    ]
  }
}
```

`scanned_files` 在 summary 的查询级是文件数；顶层 `scan.scanned_files` 是各查询文件数之和（文件次，不是去重文件数），用于表达整个 workload 的累计扫描量。

### 5.6 `layout_evaluation`

分区布局评估段落，回答“移除某个/某些分区列后各查询的裁剪收益如何变化”。

| 字段 | 含义 |
| --- | --- |
| `table` | 表名、分区列、当前分区数/文件数/字节数、单文件分区占比 |
| `columns[]` | 每个分区列的基数、逐值文件/字节/行分布、文件数与字节数分布统计、单文件分区数及占比 |
| `layouts[]` | 布局列表；`layouts[0].layout_id` 恒为 `current`，其后为各个反事实布局 |
| `layouts[].merged_group_count` | 由多个旧分区合并而来的新分区个数 |
| `layouts[].queries[]` | 每条查询在该布局下的重裁剪结果 |
| `layouts[].totals` | 各查询候选文件/字节合计及相对当前布局的 delta（`current` 无 delta） |

`layouts[].queries[]` 字段：

| 字段 | 含义 |
| --- | --- |
| `referenced_partition_columns` | 查询谓词引用的分区列（按当前表声明） |
| `retained_partition_filter_columns` | 该布局中仍能用于分区过滤的被引用列 |
| `removed_partition_filter_columns` | 因移除而失去的过滤列 |
| `partition_count` / `selected_partition_count` / `unknown_partition_count` | 新布局下的分区计数 |
| `partition_prune_ratio` | `(partition_count - selected_partition_count) / partition_count`，即 `partition_pruner` 的分区计数口径 |
| `candidate_files` / `candidate_bytes` / `candidate_rows` | 保留新分区的真实文件集合求和 |
| `candidate_file_prune_ratio` | `(table_files - candidate_files) / table_files`，与审计报告查询级 `partition_prune_ratio` 同口径 |
| `conservative_all_partitions` | 是否因失去唯一分区过滤能力而保守保留全部新分区 |
| `*_delta` | 相对当前布局的变化；`current` 中全部为 0 |
| `reasons` | 稳定原因码列表，见 `docs/DESIGN.md` 3.9 |

反事实布局按移除子集排序：先按子集大小，再按列名元组字典序。`layout_id` 为 `without:<列1>,<列2>`。所有浮点字段最多 6 位小数，输出完全确定。

## 6. SQLite ledger

默认路径 `var/ledger.sqlite`。默认要求路径位于当前工作目录内；显式 `--allow-ledger-outside-workdir` 才能使用外部路径。schema 由写入模式创建；server 只读打开已有库。

### 6.1 `runs`

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `run_id` | TEXT PK | 运行标识，正则见 workload/`validate_run_id` |
| `created_at` | TEXT | 运行时间（唯一允许变化的报告时间字段） |
| `table_name` | TEXT | 表名 |
| `lake_name` | TEXT | lake root 目录名 |
| `workload_file` / `config_file` | TEXT | 输入文件名 |
| `query_count` / `diagnostic_count` / `critical_count` / `warning_count` / `info_count` | INTEGER | 计数 |
| `table_files` / `table_bytes` | INTEGER | 表规模 |
| `scanned_files` / `scanned_bytes` / `estimated_matched_bytes` | INTEGER | 汇总扫描和命中 |
| `scan_amplification` | REAL | 汇总放大 |
| `summary_json` | TEXT | 完整 summary JSON |

### 6.2 `queries`

主键 `(run_id, query_id)`。列：scanned_files、scanned_bytes、scanned_rows、estimated_matched_bytes、scan_amplification、partition_prune_ratio、stats_prune_ratio、diagnostic_count、details_json。

`details_json` 包含 id、where、metrics、选中/总分区数和 diagnostic_ids。

### 6.3 `diagnostics`

主键 `(run_id, seq)`。列：diagnostic_id、rule_id、severity、query_id、message、evidence_json、suggestion。

### 6.4 `artifacts`

主键 `(run_id, name)`。列：sha256、byte_size。name 为四个报告文件名。

### 6.5 `input_fingerprints`

主键 `(run_id, kind)`。kind 为 `manifest` / `workload` / `config`，sha256 为输入指纹。

### 6.6 `layout_evaluations`

主键 `run_id`。列：`payload_json`，存储完整的 `layout_evaluation` 对象（与 `summary["layout_evaluation"]` 一致，规范 JSON 排序）。没有布局评估时该 run 不写入行；读取时为 `null`。`get_run(run_id)` 的结果包含 `layout_evaluation`，只读 server 在 run 详情页渲染同一段落。

### 6.7 幂等与读取

- 外键启用 `ON DELETE CASCADE`；写入前按 `diagnostics → queries → artifacts → input_fingerprints → layout_evaluations → runs` 顺序删除同 run_id 的旧记录，再插入新记录，全部在同一事务内。
- 对同一 run_id 重跑：行数不变，内容替换为该次结果。
- `recent_runs(limit)` 按 created_at、run_id 倒序读取。
- `get_run(run_id)` 返回 runs、queries、diagnostics、artifacts、fingerprints、layout_evaluation 的组合视图；只读 server 使用该视图。
- 不提供更新单条记录、删除 run、写页面或调度接口。

## 7. 错误对象

CLI 不输出 Python traceback 作为正常用户错误。`ScanAmpError` 字段：

| 字段 | 含义 |
| --- | --- |
| `message` | 人类可读错误 |
| `location` | 文件路径或 `query:<id>` |
| `field` | 出错字段 |
| `reason` | 稳定错误码，例如 `PATH_TRAVERSAL`、`TYPE_MISMATCH`、`STATS_MISSING` |
| `details` | 机器可读补充证据 |

常见 reason：

- 配置：`UNKNOWN_KEY`、`UNKNOWN_RULE`、`INVALID_SEVERITY`、`INVALID_THRESHOLD`、`UNSUPPORTED_SCHEMA`。
- manifest：`INVALID_PATH`、`PATH_TRAVERSAL`、`PATH_ESCAPE`、`FILE_NOT_FOUND`、`BYTE_SIZE_MISMATCH`、`PARTITION_SCHEMA_MISMATCH`、`INVALID_STATS_RANGE`、`DUPLICATE_PATH`。
- workload/谓词：`INVALID_QUERY_ID`、`DUPLICATE_QUERY_ID`、`UNKNOWN_COLUMN`、`TYPE_MISMATCH`、`INVALID_IS_NULL`、`PARSE_ERROR`。
- 安全：`NON_LOCAL_BIND`、`INVALID_PORT`、`INVALID_RUN_ID`、`OUTSIDE_WORKDIR`。
- ledger：`OPEN_ERROR`、`WRITE_ERROR`、`READ_ERROR`、`NOT_FOUND`。
