# Iceberg REST Catalog → Prometheus Exporter

**Purpose:**
A production-grade Prometheus exporter that derives all metrics *only* from the Iceberg REST catalog (no Trino, no Glue, no logs). The exporter polls the REST catalog and manifests via PyIceberg (or direct REST where necessary), computes snapshot deltas and aggregates, and exposes metrics at `/metrics` in Prometheus text format.

---

## Table of contents
1. Goals & constraints
2. Overview of architecture
3. Configuration
4. Metric catalog (detailed metric-by-metric spec)
5. Data sources & PyIceberg mapping (which object/field supplies each metric)
6. Collection strategy and caching
7. Cardinality & guardrails
8. Heuristics (rewrite/compaction detection, compaction candidate)
9. Prometheus & Grafana integration (PromQL snippets, recording rules, alerts)
10. Implementation plan & Python skeleton (collector classes)
11. Testing & validation
12. Deployment notes (k8s, resources)
13. Operational runbook & troubleshooting checklist

---

## 1. Goals & constraints
- **Single source of truth:** only the Iceberg REST catalog (via PyIceberg or direct REST). No external logs.
- **Scale-aware:** handle very large tables by using a two-mode collection strategy (cheap quick scrape, periodic deep scan).
- **Low cardinality by default:** exporters should avoid per-file labels; per-partition metrics allowed but guarded by settings.
- **Metric semantics suitable for Prometheus/Grafana:** use counters for cumulative changes and gauges for instantaneous values.

---

## 2. Architecture overview

- **Main components:**
  - `HTTP server` exposing `/metrics` and `/healthz`.
  - `Prometheus client registry` (prometheus_client) to register metrics.
  - `SnapshotSummaryCollector` (fast): reads table snapshot summaries and basic totals.
  - `ManifestStatsCollector` (deep, cached): traverses manifests/manifest entries and computes partition/file aggregates.
  - `RewriteInferenceCollector` (uses snapshot history + manifest stats cache) to infer rewrite/compaction events.
  - `Cache` (in-memory with TTL + optional optional persistent caching backend): stores results of deep scans.
  - `Config` for table discovery or whitelist, thresholds, and scrape behaviour.

- **Scrapes vs polls:** exporter is polled by Prometheus at `/metrics`. Internally the exporter will poll the REST catalog on its own schedule (fast vs deep) and serve the last cached values to Prometheus scrapes.

---

## 3. Configuration (YAML / env)

**Config file (example `config.yaml`):**

```yaml
# general
listen_address: 0.0.0.0
listen_port: 9104
metrics_path: /metrics
health_path: /healthz
# Catalog connection
catalog:
  type: rest
  url: https://iceberg-catalog.mycompany.internal
  auth:
    type: token
    token: ${ICEBERG_TOKEN}
# tables: either explicit list or discovery
tables:
  explicit:
    - catalog: prod
      namespace: sales
      table: events
  discover:
    enabled: false
    namespace_whitelist: [sales, marketing]
# collection schedule
collection:
  fast_poll_interval_seconds: 60      # cheap snapshot summaries
  deep_poll_interval_seconds: 900     # deep manifest scans
  deep_poll_jitter_seconds: 30
# partitions
partition_export:
  enabled: true
  max_partitions_per_table: 500  # cardinality guard
  include_partition_label: true
# small file thresholds (bytes)
file_thresholds:
  small_file_bytes: 33554432   # 32MB
  compaction_target_bytes: 134217728  # 128MB
# heuristics
heuristics:
  rewrite_detection:
    enabled: true
    min_removed_files: 1
    net_records_delta_ratio: 0.01
    min_post_avg_increase_ratio: 1.1
# exporter
exporter:
  enable_table_discovery: false
  max_tables_per_scrape: 200
# caching
cache:
  ttl_seconds: 1200 # keep deep scan results for 20 minutes
  persist_to_disk: false

# operational
log_level: INFO

```

**Env variables** usually used: `ICEBERG_TOKEN`, `PYICEBERG_CONFIG` etc.

---

## 4. Metric catalog (detailed)

> For each metric: `name | type | labels | help | source (PyIceberg object) | compute`.

### A. Snapshot / table state (gauges)

1. `iceberg_snapshot_total_data_files` | gauge
   - labels: `{catalog, namespace, table}`
   - help: Total active data files in latest snapshot.
   - source: `Table.current_snapshot()` → summary or computed from manifest entries where `status=added` and data file type.
   - compute: read snapshot summary if available: `summary.get('total-data-files')` or sum active manifests' data file counts.

2. `iceberg_snapshot_total_delete_files` | gauge
   - labels: `{catalog, namespace, table}`
   - help: Total active delete files in latest snapshot.
   - source: snapshot summary or manifests (manifest entries with `is_delete` flag).

3. `iceberg_snapshot_total_records` | gauge
   - labels: `{catalog, namespace, table}`
   - help: Total record count in latest snapshot (derived from manifest-level `record_count` aggregated).
   - source: manifests or snapshot summary `total-records`.

4. `iceberg_snapshot_total_file_size_bytes` | gauge
   - labels: `{catalog, namespace, table}`
   - help: Total bytes in active data files.
   - source: sum of `file_size_in_bytes` from manifest entries (only active files).

5. `iceberg_table_current_snapshot_id` | gauge
   - labels: `{catalog, namespace, table}`
   - help: Current snapshot id (numeric). Useful to detect advancement.
   - source: `Table.current_snapshot().snapshot_id`

6. `iceberg_table_snapshot_age_seconds` | gauge
   - labels: `{catalog, namespace, table}`
   - help: Age in seconds since current snapshot timestamp.
   - compute: `now - Table.current_snapshot().timestampMillis/1000`.

7. `iceberg_table_snapshot_count` | gauge
   - labels: `{catalog, namespace, table}`
   - help: Number of snapshots retained in metadata history.
   - source: `Table.snapshots()` length / available API for snapshot history.

8. `iceberg_table_partition_count` | gauge
   - labels: `{catalog, namespace, table}`
   - help: Number of partitions (distinct partition values) in current snapshot.
   - compute: derived from manifest entries grouped by partition. **Guarded** by `max_partitions_per_table`.


### B. Snapshot deltas / counters

1. `iceberg_snapshot_added_records_total` | counter
   - labels: `{catalog, namespace, table}`
   - help: Cumulative records added (derived from snapshot diffs observed by exporter).
   - compute: when exporter sees a newer snapshot, compare `added-records` from summary or compute `added_records = sum of added manifest entries record_count`. Increment this counter by `added_records`.

2. `iceberg_snapshot_added_data_files_total` | counter
   - labels: `{catalog, namespace, table}`
   - help: Cumulative data files added by observed snapshots.
   - compute: from snapshot summary or manifest entries diff.

3. `iceberg_snapshot_added_delete_files_total` | counter
   - labels: `{catalog, namespace, table}`
   - help: Cumulative delete files added.

4. `iceberg_snapshot_added_files_size_bytes_total` | counter
   - labels: `{catalog, namespace, table}`
   - help: Cumulative bytes added.

5. `iceberg_snapshot_removed_records_total` | counter
6. `iceberg_snapshot_removed_data_files_total` | counter
7. `iceberg_snapshot_removed_delete_files_total` | counter
8. `iceberg_snapshot_removed_files_size_bytes_total` | counter

- Notes: counters are only incremented after exporter detects a newer snapshot than last seen, so you must persist (in-memory or on-disk) the last seen snapshot id per table to compute diffs.

9. `iceberg_snapshot_changed_partition_count` | gauge
   - labels: `{catalog, namespace, table}`
   - help: number of partitions touched in latest snapshot.
   - compute: compare partition keys present in added/removed manifest entries in the snapshot summary/diff.

10. `iceberg_snapshot_commits_total{operation}` | counter
   - labels: `{catalog, namespace, table, operation}`
   - help: counts commit types observed (append/overwrite/rewrite/etc.).
   - compute: operation = snapshot summary `operation` if present; otherwise heuristics on manifest diffs.


### C. Partition metrics (gauges)

- Preferred to export **per-partition** metrics for top-k and pie charts but **guarded** by `max_partitions_per_table`.

1. `iceberg_partition_record_count{partition}` | gauge
   - labels: `{catalog, namespace, table, partition}`
   - help: record count in that partition.
   - source: aggregate manifest entry `record_count` for partition.

2. `iceberg_partition_file_count{partition}` | gauge
   - labels: `{catalog, namespace, table, partition}`
   - help: number of data files in that partition.

3. `iceberg_partition_size_bytes{partition}` | gauge
   - help: aggregated size in bytes.

4. Pre-aggregates (table-level):
   - `iceberg_partitions_min_record_count`, `_max_`, `_avg_`
   - `iceberg_partitions_min_file_count`, `_max_`, `_avg_`
   - `iceberg_partitions_min_size_bytes`, `_max_`, `_avg_`

- compute pre-aggregates from per-partition stats but allow the exporter to compute directly during deep scan to reduce PromQL needs.


### D. Files aggregates (gauges)

1. `iceberg_files_min_record_count` | gauge
2. `iceberg_files_max_record_count` | gauge
3. `iceberg_files_avg_record_count` | gauge
4. `iceberg_files_min_size_bytes` | gauge
5. `iceberg_files_max_size_bytes` | gauge
6. `iceberg_files_avg_size_bytes` | gauge
7. `iceberg_files_small_file_count{threshold_bytes}` | gauge
   - count of files smaller than `threshold_bytes` (export threshold as label or provide fixed thresholds)
8. `iceberg_files_large_file_count{threshold_bytes}` | gauge
9. `iceberg_files_delete_file_ratio` | gauge
   - `delete_file_count / max(data_file_count,1)`
10. `iceberg_files_avg_rows_per_file` | gauge

Notes: keep file-related metrics aggregated only. Avoid per-file series.


### E. Rewrite / compaction inferred counters & health (derived)

1. `iceberg_rewrite_data_files_total` | counter
   - cumulative number of data files that were removed/replaced by rewrite-like snapshots observed by exporter.

2. `iceberg_rewrite_bytes_total` | counter
   - cumulative bytes of removed data files in rewrite-like snapshots.

3. `iceberg_rewrite_snapshots_total` | counter
   - count of rewrite-like snapshots observed.

4. `iceberg_rewrite_last_timestamp_seconds` | gauge
   - timestamp of last rewrite-like snapshot.

5. `iceberg_rewrite_post_avg_file_size_bytes` | gauge
   - average file size after latest rewrite-like snapshot.

6. `iceberg_table_compaction_candidate` | gauge (0|1)
   - exporter sets to 1 if heuristic thinks compaction is recommended.

7. `iceberg_rewrite_removed_small_files_total` | counter
   - count of small files removed during rewrite-like snapshots.


### F. Exporter internals & health

1. `iceberg_exporter_up` | gauge (1/0)
   - 1 if exporter can reach catalog and last fast poll succeeded.

2. `iceberg_exporter_last_scrape_timestamp_seconds` | gauge
3. `iceberg_exporter_scrape_duration_seconds` | histogram or gauge
4. `iceberg_exporter_deep_scan_last_run_timestamp_seconds` | gauge
5. `iceberg_exporter_deep_scan_duration_seconds` | histogram/gauge

---

## 5. Data sources & PyIceberg mapping (practical)

> The following map describes which PyIceberg object or REST payload to use for the metric.

- **Table**: `Table.load(wrapped_catalog, identifier)` → table metadata and `current_snapshot()`.
- **Snapshot**: `table.current_snapshot()` or `table.snapshots()` → snapshot id, timestamp, summary fields.
- **Snapshot summary**: `snapshot.summary` (contains keys like `added-data-files`, `removed-data-files`, `added-records`, `removed-records`, `operation` depending on writer and version).
- **ManifestFile**: `snapshot.manifests` (list of manifest files) – each ManifestFile contains counts and metrics.
- **ManifestEntry**: entries inside manifests: fields `status` (added, existing, deleted), `dataFile.fileSizeInBytes`, `dataFile.recordCount`, `partition` fields etc.

**Implementation note:** Some iceberg catalogs may not include every summary key in older writer versions. Use manifests/manifest entries as fallback to compute precise values.


---

## 6. Collection strategy & caching

**Two-mode collection** (recommended):

- **Fast poll (every `fast_poll_interval_seconds`)**
  - tasks: fetch table current snapshot, snapshot id, snapshot timestamp, snapshot summary (if present), small number of manifest-level totals (if summary incomplete), minimal counters update (snapshot_commits_total), update `iceberg_exporter_up`.
  - cost: cheap (only metadata) and safe to run frequently.
  - exposed on every scrape via cache.

- **Deep scan (every `deep_poll_interval_seconds`)**
  - tasks: traverse manifests and manifest entries for each table (or for top-N tables), compute partition-level aggregates, per-file aggregates (only aggregated stats), detect rewrite-like snapshots using snapshot history, compute compaction candidate heuristics.
  - cost: expensive (I/O heavy). Must be rate-limited and cached.
  - algorithm: incremental manifest processing — read only manifests referenced by new snapshots since last deep-scan to avoid full table scan every time.

**Cache design:**
- in-memory dictionary keyed by `catalog:namespace:table` with timestamped content and TTL = `cache.ttl_seconds`.
- optional persistence to disk (sqlite/leveldb) to survive restarts so counters can be preserved.
- deep scan results should persist across restarts if possible (to avoid recomputing heavy aggregates and to properly compute counters).

**Snapshot-diff persistence:**
- exporter needs to remember last-seen snapshot id per table to compute added/removed counters reliably. Persist to disk atomically, or write to a small sqlite file.

**Concurrency & rate limiting:**
- deep scan tasks should be executed with a thread pool of size configurable (e.g., 4) and a per-table concurrency guard.
- add jitter to deep scan schedule to avoid stampedes across many exporter instances.

**Scrape handling:**
- when Prometheus scrapes `/metrics`, exporter returns current registry values from cache immediately. Scrapes should not block until deep scan completes.

---

## 7. Cardinality & guardrails

**Avoid cardinality explosion:**
- default: do not export per-file series.
- partition label: guarded by `max_partitions_per_table` (default 500). If a table has more partitions, either:
  - export only top-N partitions by `file_count`/`record_count`, or
  - disable partition-level metrics for that table and only expose pre-aggregates.

**Labels to always include:** `catalog`, `namespace`, `table`.
**Labels to avoid unless explicitly enabled:** `partition` (enable with guard), `spec_id` (only if necessary), `file_id` (never by default).

**Threshold parameters:** small file thresholds should be configurable. Export small_file_count as aggregated counts grouped by threshold.

**Max tables per scrape:** avoid scanning thousands of tables per deep scan — have `max_tables_per_scrape` config and prioritization by `last_activity` or `table_size` (if known).

---

## 8. Heuristics

### A. Rewrite / compaction detection heuristic

**Primary rule:**
- if snapshot summary `operation` == `rewrite` or `operation` explicitly indicates `optimize`, treat as rewrite-like.

**Fallback heuristic:**
- `removed_data_files > 0` AND `added_data_files > 0`
- AND `abs(added_records - removed_records) / max(pre_snapshot_total_records,1) < net_records_delta_ratio` (default `0.01`)
- AND `post_avg_file_size >= pre_avg_file_size * min_post_avg_increase_ratio` (default `1.1`)

When conditions are satisfied, update these counters:
- `iceberg_rewrite_data_files_total += removed_data_files`
- `iceberg_rewrite_bytes_total += removed_data_files_size_bytes`
- `iceberg_rewrite_snapshots_total += 1`
- `iceberg_rewrite_last_timestamp_seconds = snapshot_ts`
- `iceberg_rewrite_removed_small_files_total += count(files_removed where size < small_file_threshold)`

### B. Compaction candidate heuristic
Set `iceberg_table_compaction_candidate = 1` if any:
- `iceberg_files_avg_size_bytes < compaction_target_bytes`
- OR `iceberg_files_small_file_count{threshold=small_file_bytes} > small_file_count_threshold` (default 100)
- OR `iceberg_files_delete_file_ratio > delete_file_ratio_threshold` (default 0.2)
- OR `iceberg_partitions_avg_file_count > avg_files_per_partition_threshold` (default 20)

Tune defaults per your environment.

---

## 9. Prometheus integration, recording rules & alerts

**Prometheus scrape config example:**
```yaml
scrape_configs:
  - job_name: icebergest
    scrape_interval: 60s
    metrics_path: /metrics
    static_configs:
      - targets: ['iceberg-exporter.prod.svc.cluster.local:9104']
```

**Example Recording Rules (prometheus rules file):**
```yaml
groups:
- name: iceberg_recordings
  rules:
  - record: iceberg:partition_file_count:avg5m
    expr: avg_over_time(iceberg_partition_file_count[5m])
  - record: iceberg:files_small_count:sum5m
    expr: sum(iceberg_files_small_file_count) by (namespace,table)
  - record: iceberg:rewrite_bytes:rate1h
    expr: increase(iceberg_rewrite_bytes_total[1h])
```

**Example Alerts:**
```yaml
groups:
- name: iceberg_alerts
  rules:
  - alert: IcebergExporterDown
    expr: iceberg_exporter_up == 0
    for: 2m
    labels: {severity: critical}
    annotations: {summary: "Iceberg exporter unreachable"}

  - alert: IcebergTableNeedsCompaction
    expr: iceberg_table_compaction_candidate == 1
    for: 30m
    labels: {severity: warning}
    annotations:
      summary: "{{ $labels.namespace }}.{{ $labels.table }} likely needs compaction"

  - alert: IcebergSnapshotStalled
    expr: time() - iceberg_table_snapshot_age_seconds > 86400
    for: 1h
    labels: {severity: warning}
    annotations: {summary: "Snapshot old > 24h"}
```

**Grafana mapping tips:**
- For `snapshot.added_*` use `increase(iceberg_snapshot_added_records_total[5m])`.
- For top partitions use `topk(10, iceberg_partition_file_count{namespace="DBNAME",table="TABLENAME"})`.
- For pie charts use `sum by (partition) (iceberg_partition_file_count{...})`.

---

## 10. Implementation plan & Python skeleton

**Dependencies:**
- `pyiceberg` (or direct REST HTTP client if you prefer raw REST).
- `prometheus_client` (official Python client)
- HTTP server (built-in from prometheus_client or `aiohttp` if async recommended)
- optional: `sqlite` for persistence

**High-level classes:**
- `ExporterApp` (main) – config, scheduler, prometheus registry
- `SnapshotSummaryCollector` – cheap collector, registered with prometheus_client
- `ManifestStatsCollector` – deep collector, runs async/pool, provides aggregated results
- `RewriteInferenceCollector` – reads snapshot history, updates rewrite counters
- `CacheManager` – in-memory + optional persistent store
- `TableScanner` – wraps PyIceberg calls for a single table
- `PersistedState` – small sqlite for last_seen_snapshot per table

**Example layout:**
```
exporter/
  __init__.py
  main.py
  config.py
  collectors/
    __init__.py
    snapshot_summary.py
    manifest_stats.py
    rewrite_inference.py
  storage/
    cache.py
    persisted_state.py
  http/
    server.py
  utils/
    pyiceberg_wrapper.py
    metrics_helper.py
  deploy/
    k8s-deployment.yaml
```

**Simple collector example (pseudo-code):**

```python
# collectors/snapshot_summary.py
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily

class SnapshotSummaryCollector:
    def __init__(self, config, cache, pyiceberg_client):
        self.config = config
        self.cache = cache
        self.client = pyiceberg_client

    def collect(self):
        # For each table in config
        for t in self.config.tables:
            key = f"{t.catalog}:{t.namespace}:{t.table}"
            data = self.cache.get(key, "fast")
            # data should include totals from last fast poll
            labels = [t.catalog, t.namespace, t.table]

            g = GaugeMetricFamily(
                'iceberg_snapshot_total_data_files',
                'Total active data files in latest snapshot',
                labels=['catalog','namespace','table']
            )
            g.add_metric(labels, data['total_data_files'])
            yield g

            # counters need to be registered using CounterMetricFamily
            c = CounterMetricFamily(
                'iceberg_snapshot_added_data_files_total',
                'Cumulative data files added',
                labels=['catalog','namespace','table']
            )
            c.add_metric(labels, data['added_data_files_total'])
            yield c
```

**Deep manifest scan & detection (pseudo):**

```python
# collectors/manifest_stats.py
# run in background thread every deep_poll_interval_seconds
for table in tables_to_scan:
    table_obj = pyiceberg_client.load_table(table_identifier)
    # iterate manifests for snapshots newer than last deep scan
    manifests = table_obj.current_snapshot().manifests
    for manifest in manifests:
        for entry in manifest.entries:
            # aggregate per-partition data
            partition_key = serialize_partition(entry.data_file.partition)
            part_stats[partition_key].files += 1
            part_stats[partition_key].records += entry.data_file.record_count
            part_stats[partition_key].bytes += entry.data_file.file_size_in_bytes
    # compute file-level aggregates, small file counts
    compute_and_cache_result(table, part_stats, file_aggregates)
```

**Persist last seen snapshot example:**
- Use sqlite table `last_seen_snapshots(catalog,namespace,table,last_snapshot_id,last_checked_ts)`.
- Update atomically after deep scan or whenever new snapshot detected.


---

## 11. Testing & validation

**Unit tests:**
- mock PyIceberg Table/Snapshot/Manifest objects and assert metrics computed.
- test rewrite detection heuristics against crafted snapshot pairs.

**Integration tests:**
- run exporter against a mini Iceberg REST catalog or local test catalog with small tables.
- assert `/metrics` contains expected time series.

**Performance tests:**
- run deep scan on large synthetic table (e.g., 1M manifest entries) and measure deep_scan_duration_seconds.
- tune thread pool and manifests pagination.

**Test data:**
- create snapshots representing `append`/`overwrite`/`rewrite`/`delete` and verify counters.

---

## 12. Deployment notes (k8s)

**Kubernetes deployment example (high-level):**
- container image: `docker.io/org/iceberg-exporter:1.0`
- resources: `requests: cpu 200m mem 256Mi`, `limits: cpu 1, mem 1Gi` (tune per deep-scan activity)
- readiness/liveness: `/healthz`
- configmap for `config.yaml`, secret for `ICEBERG_TOKEN`.
- HorizontalPodAutoscaler: not recommended unless you run many deep scans; prefer single instance scaled by Prometheus scrape.

**Prometheus service discovery:**
- Expose service `iceberg-exporter` and add `scrape_config` pointing at it.

**RBAC & network:**
- exporter must be able to reach the Iceberg REST endpoint with required auth.

---

## 13. Operational runbook & troubleshooting

**If exporter_up == 0:**
- check network to catalog
- check token/credentials
- check logs for PyIceberg exceptions

**Deep scan slow:**
- increase `deep_poll_interval_seconds`
- reduce `max_tables_per_scrape`
- increase thread pool size if IO bound

**Counters not increasing:**
- verify `last_seen_snapshot_id` persisted and correct
- inspect snapshot summary fields existence

**Cardinality high:**
- disable partition export or raise `max_partitions_per_table`

---

## Appendix: Recommended defaults (tunable)

- `fast_poll_interval_seconds`: 60
- `deep_poll_interval_seconds`: 900 (15 min)
- `max_partitions_per_table`: 500
- `small_file_bytes`: 32MB
- `compaction_target_bytes`: 128MB
- `small_file_count_threshold`: 100
- `delete_file_ratio_threshold`: 0.2

---

*If you want, I can now produce:*
- a ready-to-run **Python exporter skeleton** (async, using pyiceberg + prometheus_client) implementing the above collectors and caching logic, including a Dockerfile and k8s manifest; **or**
- complete **Grafana dashboard JSON** mapping each Datadog widget to PromQL using these metrics.

Tell me which next (I recommend the Python exporter skeleton).

