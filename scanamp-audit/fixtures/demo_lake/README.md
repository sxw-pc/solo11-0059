# Demo lake fixture

This directory is a tiny, fully synthetic lake table used by the scanamp-audit
demo. It contains no real data, credentials, domains or customer identifiers.

- Table name: `demo_events`
- Partition columns: `dt` (date), `region` (string)
- Data columns: `user_id` (integer), `event_type` (string), `amount` (float),
  `status` (string)
- Files: six small CSV samples across five logical partitions.
- `_metadata/manifest.yaml` is the authoritative file inventory and sidecar
  column-statistics catalog. The CSV files are sample content only; the
  auditor never parses their bytes beyond checking file existence and size.

Intentional fixture quirks for the demo workload:

- `dt=2024-01-01/region=beta/part-0001.csv` is declared in the manifest with
  `region=delta`, producing a deliberate `PATH_MANIFEST_MISMATCH` diagnostic.
- `user_id` has `has_stats: false` in the three alpha-partition files, so
  queries on `user_id` trigger `STATS_MISSING` and `LOW_SELECTIVITY_PARTITION`.
- `substr(dt, 1, 7)` in the demo workload triggers
  `FUNCTION_WRAPPED_PARTITION` and `UNKNOWN_PREDICATE_CONSERVATIVE`.
- The OR query is crafted so its union scans twice the largest branch.
