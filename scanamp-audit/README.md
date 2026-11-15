# 扫描放大审计台（scanamp-audit）

面向本地小型数据湖/数仓表目录的**离线静态扫描放大审计器**。给定表清单、分区布局、文件级 sidecar 列统计和一批查询谓词，回答三个问题：

1. 查询实际可能扫描多少分区、文件和字节？
2. 分区裁剪或文件级统计裁剪为什么失效？
3. 这次审计的证据是否可复核、可归档、可回归？

项目只做静态分析与证据生成，不执行 SQL，不连接数据库，不读取真实 Parquet/ORC/Avro 内容。

## 是 / 不是

**是**

- 静态审计本地表目录的文件清单、分区路径、分区值和 sidecar 列统计。
- 解析查询谓词，做分区级候选裁剪和文件级统计裁剪。
- 计算扫描放大、裁剪比例、OR 分支扩张等指标。
- 生成确定性 JSON / Markdown / 自包含 HTML 报告和 SQLite 运行台账。
- 用离线 fixture、golden summary 和 pytest 回归证明结果可复现。

**不是**

- 不是表维护、小文件合并、孤儿文件清理或 manifest 重写工具。
- 不是快照、时间旅行、版本回溯、冷热迁移或归档执行工具。
- 不是 schema 演进检查、数据抽样、脱敏、血缘、指标口径或质量规则引擎。
- 不是调度器、爬虫、RPA、浏览器自动化、通用 BI 看板、权限后台或登录系统。
- 不是查询执行器：不生成执行计划、不跑 SQL、不连接数据库或查询引擎、不调用云对象存储或外部 API。
- 不执行任何自动优化：不改分区、不写回表、不自动重写统计。

## 完全离线与合规

- 运行期不发起网络请求，包括 DNS、HTTP、HTTPS、SMTP、SSH。
- `serve` 只接受 `127.0.0.1`、`localhost`、`::1`；其他地址直接拒绝。
- 报告和页面自包含：无 CDN、外部字体、外部图片、外部脚本；动态文本默认 HTML 转义。
- manifest 中的文件路径必须是表根目录内的相对 POSIX 路径；绝对路径、盘符、`.`/`..`、空路径和 NUL 字节会报错并记录证据。
- 不写真实密钥、令牌、密码、私钥、真实公司名、真实域名或公网目标地址。
- fixture 是合成 CSV 和 sidecar YAML 统计，不包含真实数据或凭据。
- ledger 是一次审计的只读证据索引；查看页面只支持 GET/HEAD，不提供写接口。

## 快速开始

要求 Python 3.11+。依赖为 Flask 3.x、PyYAML 6.x、pytest 8.x（pytest 仅开发测试使用）。

```bash
# 在项目根目录安装（离线环境请使用本地 wheel 缓存或已安装的依赖）
python -m pip install ".[dev]"

# 1. 运行内置 demo，生成报告并写入 SQLite 台账
python -m scanamp_audit demo --out var/demo-run

# 2. 运行完整测试
pytest -q

# 3. 启动只读本地台账浏览器，然后访问 http://127.0.0.1:8765/
python -m scanamp_audit serve --ledger var/ledger.sqlite --host 127.0.0.1 --port 8765

# 4. 对比 golden summary（除运行时间外的确定性回归）
python -m scanamp_audit golden --actual var/demo-run/summary.json --expected tests/golden/expected_summary.json
```

安装后也可以直接使用 `scanamp-audit` 命令，参数与 `python -m scanamp_audit` 相同。

## Demo 预期结果

`demo` 会审计 5 个分区、6 个文件、570 行、497 字节的合成事件表，运行 8 条查询，并故意覆盖全部 8 条诊断规则：

```text
run_id=demo-run report_dir=var/demo-run queries=8 diagnostics=13 critical=2 warning=7
```

当前 fixture 下的关键证据：

- `q03_function_dt` 使用 `substr(dt, 1, 7)`，分区列被函数包裹，触发 `FUNCTION_WRAPPED_PARTITION`（critical）和保守扫描诊断；查询结果、summary 查询条目和 `UNKNOWN_PREDICATE_CONSERVATIVE` 的 evidence 中附带 `degraded_path_rewrite` 改写建议块（本例域内分区全部命中，保守上界为全表 497 字节）。
- `q05_missing_user_stats` 的 `user_id` 在 alpha 分区文件上没有统计，统计裁剪失效并触发 `STATS_MISSING`。
- `q04_or_branches` 的两个 OR 分支落在不同分区，合并后扫描集合大于任一分区，触发 `OR_BRANCH_BLOWUP`。
- `dt=2024-01-01/region=beta/part-0001.csv` 的 manifest 分区值是 `delta`，与路径推断值不一致，触发 `PATH_MANIFEST_MISMATCH`（critical）。
- `q07_null_status` 只过滤数据列，没有任何分区过滤，放大倍数为 `64.75`，同时触发 `PARTITION_KEY_UNFILTERED`。
- 汇总扫描放大为 `1.851562`，扫描 28 个文件次 / 2370 字节，估计命中 1280 字节。

`summary.json` 不包含运行时间字段，因此相同输入、相同配置、相同 `--run-id` 会产生逐字节相同的 summary。`report.json`、`report.md`、`report.html` 中的 `generated_at` 是唯一允许变化的运行时字段。

## 输入目录约定

```text
<lake-root>/
  _metadata/
    manifest.yaml
  dt=2024-01-01/
    region=alpha/
      part-0001.csv
```

- `_metadata/manifest.yaml` 声明表名、列类型、分区列和每个文件的相对路径、行数、字节数、分区值及 sidecar 列统计。
- 文件路径只允许位于 `<lake-root>` 内；路径中的 Hive 风格 `key=value` 目录会被推断为分区值，用于与 manifest 的 `partition_values` 对比。
- 不解析 CSV 数据内容；`row_count`、`byte_size` 和列统计由 manifest 声明并校验。
- workload YAML 提供查询 id、描述和 `where` 谓词。
- audit config YAML 控制诊断开关、严重级别、阈值、默认选择率和报告排名长度。

完整字段与约束见 [docs/SCHEMA.md](docs/SCHEMA.md)。

### 支持的谓词子集

`where` 支持：

- 比较：`=`、`!=`、`<>`、`<`、`<=`、`>`、`>=`。
- 布尔组合：`AND`、`OR`、`NOT`、括号；优先级为 `NOT` > `AND` > `OR`。
- `IN (...)`、`NOT IN (...)`。
- `IS NULL`、`IS NOT NULL`。
- `BETWEEN ... AND ...`、`NOT BETWEEN ... AND ...`。
- 字符串、数字、ISO 日期字面量和带负号的数字。
- 函数调用形式（例如 `substr(dt, 1, 7)`）。函数不会被求值为真实 SQL，只用于静态识别“分区列被函数包裹”的裁剪失效原因。

类型校验在加载 workload 时完成：未知列、类型不匹配、非法日期格式和非法 `IS NULL` 操作数都会报错。

## 命令行

```bash
python -m scanamp_audit demo  [--out var/demo-run] [--ledger var/ledger.sqlite] [--run-id demo-run] [--fail-on none|info|warning|critical] [--allow-ledger-outside-workdir]
python -m scanamp_audit audit --lake LAKE --workload WORKLOAD --config CONFIG [--out var] [--ledger var/ledger.sqlite] [--run-id RUN_ID] [--fail-on ...] [--allow-ledger-outside-workdir]
python -m scanamp_audit serve [--ledger var/ledger.sqlite] [--host 127.0.0.1] [--port 8765] [--allow-ledger-outside-workdir]
python -m scanamp_audit golden --actual SUMMARY.json --expected GOLDEN.json
```

- `demo` 直接写入 `--out` 指定目录，默认 `var/demo-run`；默认 run id 为 `demo-run`。
- `audit` 写入 `<out>/runs/<run_id>/`；未指定 run id 时按 manifest/workload/config 指纹生成 `run-<manifest8>-<workload8>-<config8>`。
- `serve` 只读打开 ledger；ledger 不存在时报错，不会创建空库。
- 退出码：`0` 成功 / gate 未触发；`1` `--fail-on` 命中；`2` 输入、安全、路径或 IO 错误。

`--fail-on` 的严重级别权重为 `info=1`、`warning=2`、`critical=3`、`none=0`。命中任意达到或超过阈值的诊断时返回 `1`，可用于 CI gate。demo 默认 `none`，因此默认退出码为 `0`。

## 输出

一次运行产生四个归档产物：

| 文件 | 用途 |
| --- | --- |
| `report.json` | 完整机器可读报告：输入指纹、配置、表布局、逐查询裁剪决策、诊断和 summary |
| `report.md` | 适合代码评审和 PR 附件的 Markdown 证据报告 |
| `report.html` | 自包含 HTML 报告，内联 CSS，无外部资源，动态文本转义 |
| `summary.json` | 确定性摘要，用于排名、gate 和 golden 回归 |

`audit` 的输出目录为 `var/runs/<run-id>/`；`demo` 的输出目录为 `var/demo-run/`（可通过 `--out` 修改）。

SQLite ledger 保存：

- `runs`：运行级汇总、扫描字节、放大倍数和 summary JSON。
- `queries`：逐查询指标和裁剪详情。
- `diagnostics`：诊断消息、证据 JSON、建议和严重级别。
- `artifacts`：四个报告产物的 SHA-256 与字节数。
- `input_fingerprints`：manifest、workload、config 的指纹。

同一个 `run_id` 再次写入时会在一个事务内替换该 run 的所有子记录，保持幂等；不会生成重复 run。

## 诊断规则

| 规则 | 默认严重级别 | 触发含义 |
| --- | --- | --- |
| `PARTITION_KEY_UNFILTERED` | warning | 查询没有引用任何分区列，所有分区都是候选 |
| `LOW_SELECTIVITY_PARTITION` | warning | 分区裁剪有效，但文件统计裁剪后仍扫描大量文件 |
| `HIGH_CARDINALITY_OVERPARTITION` | info | 分区数较多且查询仍选中大部分分区 |
| `FUNCTION_WRAPPED_PARTITION` | critical | 分区列被函数包裹，无法静态裁剪 |
| `STATS_MISSING` | warning | 过滤列缺少可用 min/max 统计，文件只能保守保留 |
| `PATH_MANIFEST_MISMATCH` | critical | 路径推断的分区值与 manifest 声明不一致 |
| `OR_BRANCH_BLOWUP` | warning | OR 分支并集明显大于最大单分支扫描集合 |
| `UNKNOWN_PREDICATE_CONSERVATIVE` | warning | 不可判定谓词导致所有分区被保留 |

每条规则都可以在配置中单独关闭、调整 severity，并按文档设置阈值。详细触发条件、证据字段和修复建议见 [docs/DIAGNOSTICS.md](docs/DIAGNOSTICS.md)。

当一条查询同时命中 `FUNCTION_WRAPPED_PARTITION` 与 `UNKNOWN_PREDICATE_CONSERVATIVE` 时，查询结果会附带 `degraded_path_rewrite` 聚合建议块：按表的实际分区取值域，把函数包裹的谓词片段（含 `NOT` 链）推导为等价的裸比较写法，并给出改写后扫描字节的保守上界（不能真正裁剪时上界保持为全表字节，不会小于原查询扫描量）。建议块在 report.json 的 `queries[]`、summary 的查询条目和 `UNKNOWN_PREDICATE_CONSERVATIVE` 诊断的 evidence 中可见，顺序稳定。

## 核心语义

- **只有确定为 FALSE 才裁剪。** 分区级使用精确的分区值；文件级使用 min/max/null_count/distinct_count 统计。
- **UNKNOWN 一律保守保留。** 缺统计、函数包裹、跨类型或不可判定谓词不会误删文件。
- **NULL 语义显式处理。** `IS NULL` / `IS NOT NULL` 使用 null_count；普通比较不把 NULL 当作命中，也不因 NULL 误裁剪。
- **NOT 先转否定范式（NNF）。** 将 NOT 推到叶子并翻转比较符，避免 `NOT` 外层导致错误裁剪。
- **选择率是估计值。** 扫描字节来自文件声明大小；命中字节按选择性估计，因此放大倍数是静态估计，不是执行实测。
- **所有排序稳定。** 文件、分区、查询、诊断、排名和 JSON key 都使用确定顺序；浮点结果统一舍入。

## 项目结构

```text
.
├── pyproject.toml / requirements.txt
├── README.md
├── docs/
│   ├── DESIGN.md
│   ├── SCHEMA.md
│   └── DIAGNOSTICS.md
├── fixtures/
│   ├── demo_lake/                  # 合成湖表 + CSV + manifest
│   ├── workloads/                  # demo / regression 查询集
│   └── configs/                    # 审计配置
├── src/scanamp_audit/
│   ├── cli.py                      # demo / audit / serve / golden
│   ├── config.py                   # 阈值、severity、严格配置校验
│   ├── loader.py                   # manifest 加载、安全路径、指纹
│   ├── models.py                   # 领域模型
│   ├── schema.py                   # 类型、规范化、哈希
│   ├── security.py                 # 路径、host、run-id 安全检查
│   ├── workload.py                 # workload 加载和查询分析
│   ├── ledger.py                   # SQLite 台账
│   ├── server.py                   # 只读 Flask 页面
│   ├── expr/                       # tokenizer / parser / validate / evaluator / stats
│   ├── engine/                     # partition / file pruning / metrics / diagnostics / auditor
│   └── report/                     # JSON / Markdown / HTML / renderer
├── tests/                          # 82 个 pytest 用例 + golden
├── tests/golden/                   # 确定性 summary / diagnostics golden
└── var/                            # 运行输出与 ledger（git 忽略）
```

## 测试与回归

```bash
pytest -q
```

测试覆盖：

- tokenizer、谓词优先级、括号、NOT/IN/BETWEEN/IS NULL 和函数分析。
- 区间重叠、日期排序、NULL 三值语义。
- 分区级裁剪、文件统计裁剪、缺统计的保守保留。
- 每条诊断规则的命中与不命中、阈值和 severity 覆盖。
- manifest 路径穿越、绝对路径、字节数不一致、分区值不一致。
- JSON/Markdown/HTML 渲染、HTML 转义、无外部资源。
- ledger 幂等、run 替换、指纹和 artifact 哈希。
- CLI 集成、`--fail-on` gate、golden summary。
- Flask 只读路由、POST 405、非本地 host 拒绝。
- 离线测试：通过 socket monkeypatch 禁用连接，demo 全程不触网。

当前 fixture 的 golden 结果为 8 条查询、13 条诊断（2 critical / 7 warning / 4 info）。

## 代码规模

按 `find src tests -name '*.py'` 统计（不含 fixture、golden 和生成报告）：

- 源码 3722 行，测试 682 行，合计 4404 行，满足 2500–4500 行约束。
- 单文件最大 260 行（`src/scanamp_audit/expr/evaluator.py`），满足单文件不超过 300 行约束。
- 为满足行数预算，模块内采用紧凑的物理行风格；逻辑分层和文档仍保持独立。

## 已知限制

- 不读取 CSV/Parquet 内容；行数、字节数和统计完全依赖 manifest 声明。声明不准时审计结论也会偏。
- 选择率是基于 min/max、distinct_count、null_count 和配置默认值的静态估计，不是实际执行结果。
- 只支持文档中的谓词子集；不支持子查询、JOIN、算术表达式、聚合、`LIKE` 或任意 SQL。
- 函数调用只用于识别函数包裹的分区列，不执行函数。
- 日期按 ISO 字符串规范化，字符串统计依赖字典序；对非 ISO 日期或本地化字符串不作语义推断。
- 单机本地工具，不提供多租户、权限、登录、分布式执行或远程数据源访问。
- `serve` 是只读台账浏览器，不提供写入、删除、调度或“一键修复”功能。
- fixture 和 demo 结果只用于验证工具行为，不代表生产表或真实业务指标。

## 文档索引

- [设计说明](docs/DESIGN.md)：模块划分、数据流、确定性策略和正确性边界。
- [数据与接口契约](docs/SCHEMA.md)：manifest、workload、config、报告 JSON、SQLite 表结构。
- [诊断规则手册](docs/DIAGNOSTICS.md)：8 条规则的触发条件、证据、阈值和修复建议。
