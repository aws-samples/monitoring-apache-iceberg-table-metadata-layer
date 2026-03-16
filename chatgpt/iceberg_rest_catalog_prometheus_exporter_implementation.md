# Iceberg REST Catalog Prometheus Exporter – Ready-to-Run Skeleton + Grafana Dashboard

This document contains:
1. A ready-to-run Python exporter skeleton (single-file, production-oriented starter)
2. A complete Grafana dashboard JSON template mapping the Datadog-style widgets to PromQL using the REST-catalog-only metrics

---

## Part 1 — Ready-to-run Python exporter skeleton

### File: `iceberg_rest_exporter.py`

```python
#!/usr/bin/env python3
"""
Iceberg REST Catalog Prometheus Exporter (REST-catalog only)

Goals:
- Single source of truth: Iceberg REST catalog only
- No Trino, no Glue, no Elasticsearch
- Cheap polling loop + cached deep scan
- Exposes Prometheus metrics via prometheus_client

This is a production-oriented skeleton, not toy code.
You will still need to adapt a few PyIceberg APIs to your exact version.

Tested design assumptions:
- Python 3.11+
- prometheus-client
- pyiceberg
- PyYAML

Run:
  pip install prometheus-client pyiceberg pyyaml
  python iceberg_rest_exporter.py --config config.yaml

Prometheus endpoint:
  http://localhost:9109/metrics
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml
from prometheus_client import Counter, Gauge, start_http_server, disable_created_metrics

# NOTE:
# PyIceberg APIs evolve. You may need to adjust imports for your exact version.
# These imports are intentionally conservative and may require small edits.
from pyiceberg.catalog import load_catalog


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("iceberg-rest-exporter")


# ============================================================
# Config models
# ============================================================

@dataclass
class CatalogConfig:
    name: str
    uri: str
    warehouse: Optional[str] = None
    token: Optional[str] = None
    credential: Optional[str] = None
    extra_properties: Dict[str, str] = field(default_factory=dict)


@dataclass
class TableRef:
    namespace: str
    table: str

    @property
    def fqdn(self) -> str:
        return f"{self.namespace}.{self.table}"


@dataclass
class DiscoveryConfig:
    mode: str = "explicit"  # explicit | namespace_scan
    namespaces: List[str] = field(default_factory=list)
    tables: List[TableRef] = field(default_factory=list)


@dataclass
class ThresholdsConfig:
    small_file_bytes: int = 32 * 1024 * 1024
    target_file_bytes: int = 128 * 1024 * 1024
    delete_file_ratio: float = 0.2
    avg_files_per_partition: int = 20
    small_file_count: int = 100


@dataclass
class ExporterConfig:
    host: str = "0.0.0.0"
    port: int = 9109
    scrape_interval_seconds: int = 30
    deep_scan_interval_seconds: int = 600
    request_timeout_seconds: int = 30
    max_partitions_per_table: int = 5000
    catalogs: List[CatalogConfig] = field(default_factory=list)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    thresholds: ThresholdsConfig = field(default_factory=ThresholdsConfig)


# ============================================================
# Metric registry (explicit metric handles)
# ============================================================

LABELS = ["catalog", "namespace", "table", "table_name"]
PARTITION_LABELS = ["catalog", "namespace", "table", "table_name", "partition"]
OP_LABELS = ["catalog", "namespace", "table", "table_name", "operation"]
THRESHOLD_LABELS = ["catalog", "namespace", "table", "table_name", "threshold_bytes"]


class Metrics:
    def __init__(self) -> None:
        disable_created_metrics()

        # exporter health
        self.exporter_last_run_success = Gauge(
            "iceberg_exporter_last_run_success",
            "Whether the last exporter loop completed successfully (1/0)",
        )
        self.exporter_last_run_timestamp = Gauge(
            "iceberg_exporter_last_run_timestamp_seconds",
            "Unix timestamp of last exporter loop",
        )
        self.exporter_loop_duration = Gauge(
            "iceberg_exporter_loop_duration_seconds",
            "Duration of the last exporter loop",
        )
        self.exporter_table_scrape_errors = Counter(
            "iceberg_exporter_table_scrape_errors_total",
            "Number of per-table scrape errors",
            ["catalog", "namespace", "table", "table_name", "stage"],
        )

        # Snapshot state gauges
        self.snapshot_total_data_files = Gauge(
            "iceberg_snapshot_total_data_files",
            "Current total active data files in latest snapshot",
            LABELS,
        )
        self.snapshot_total_delete_files = Gauge(
            "iceberg_snapshot_total_delete_files",
            "Current total active delete files in latest snapshot",
            LABELS,
        )
        self.snapshot_total_records = Gauge(
            "iceberg_snapshot_total_records",
            "Current total records in latest snapshot",
            LABELS,
        )
        self.snapshot_total_file_size_bytes = Gauge(
            "iceberg_snapshot_total_file_size_bytes",
            "Current total active data file size in bytes",
            LABELS,
        )
        self.snapshot_total_position_delete_records = Gauge(
            "iceberg_snapshot_total_position_delete_records",
            "Current total position delete records in latest snapshot",
            LABELS,
        )
        self.snapshot_total_equality_delete_records = Gauge(
            "iceberg_snapshot_total_equality_delete_records",
            "Current total equality delete records in latest snapshot",
            LABELS,
        )
        self.table_current_snapshot_id = Gauge(
            "iceberg_table_current_snapshot_id",
            "Current snapshot id",
            LABELS,
        )
        self.table_current_sequence_number = Gauge(
            "iceberg_table_current_sequence_number",
            "Current sequence number",
            LABELS,
        )
        self.table_snapshot_age_seconds = Gauge(
            "iceberg_table_snapshot_age_seconds",
            "Age of current snapshot in seconds",
            LABELS,
        )
        self.table_snapshot_count = Gauge(
            "iceberg_table_snapshot_count",
            "Number of retained snapshots",
            LABELS,
        )
        self.table_partition_count = Gauge(
            "iceberg_table_partition_count",
            "Number of active partitions",
            LABELS,
        )

        # Snapshot delta counters
        self.snapshot_added_records_total = Counter(
            "iceberg_snapshot_added_records_total",
            "Cumulative added records across observed snapshots",
            LABELS,
        )
        self.snapshot_added_data_files_total = Counter(
            "iceberg_snapshot_added_data_files_total",
            "Cumulative added data files across observed snapshots",
            LABELS,
        )
        self.snapshot_added_delete_files_total = Counter(
            "iceberg_snapshot_added_delete_files_total",
            "Cumulative added delete files across observed snapshots",
            LABELS,
        )
        self.snapshot_added_files_size_bytes_total = Counter(
            "iceberg_snapshot_added_files_size_bytes_total",
            "Cumulative added file bytes across observed snapshots",
            LABELS,
        )
        self.snapshot_removed_records_total = Counter(
            "iceberg_snapshot_removed_records_total",
            "Cumulative removed records across observed snapshots",
            LABELS,
        )
        self.snapshot_removed_data_files_total = Counter(
            "iceberg_snapshot_removed_data_files_total",
            "Cumulative removed data files across observed snapshots",
            LABELS,
        )
        self.snapshot_removed_delete_files_total = Counter(
            "iceberg_snapshot_removed_delete_files_total",
            "Cumulative removed delete files across observed snapshots",
            LABELS,
        )
        self.snapshot_removed_files_size_bytes_total = Counter(
            "iceberg_snapshot_removed_files_size_bytes_total",
            "Cumulative removed file bytes across observed snapshots",
            LABELS,
        )
        self.snapshot_changed_partition_count = Gauge(
            "iceberg_snapshot_changed_partition_count",
            "Partitions changed in latest observed snapshot",
            LABELS,
        )
        self.snapshot_commits_total = Counter(
            "iceberg_snapshot_commits_total",
            "Observed snapshot commits by operation",
            OP_LABELS,
        )

        # Partition gauges
        self.partition_record_count = Gauge(
            "iceberg_partition_record_count",
            "Record count by active partition",
            PARTITION_LABELS,
        )
        self.partition_file_count = Gauge(
            "iceberg_partition_file_count",
            "Active data file count by partition",
            PARTITION_LABELS,
        )
        self.partition_size_bytes = Gauge(
            "iceberg_partition_size_bytes",
            "Active data size by partition in bytes",
            PARTITION_LABELS,
        )

        # Partition aggregate gauges
        self.partitions_min_record_count = Gauge("iceberg_partitions_min_record_count", "Min records across partitions", LABELS)
        self.partitions_max_record_count = Gauge("iceberg_partitions_max_record_count", "Max records across partitions", LABELS)
        self.partitions_avg_record_count = Gauge("iceberg_partitions_avg_record_count", "Avg records across partitions", LABELS)
        self.partitions_min_file_count = Gauge("iceberg_partitions_min_file_count", "Min file count across partitions", LABELS)
        self.partitions_max_file_count = Gauge("iceberg_partitions_max_file_count", "Max file count across partitions", LABELS)
        self.partitions_avg_file_count = Gauge("iceberg_partitions_avg_file_count", "Avg file count across partitions", LABELS)
        self.partitions_min_size_bytes = Gauge("iceberg_partitions_min_size_bytes", "Min bytes across partitions", LABELS)
        self.partitions_max_size_bytes = Gauge("iceberg_partitions_max_size_bytes", "Max bytes across partitions", LABELS)
        self.partitions_avg_size_bytes = Gauge("iceberg_partitions_avg_size_bytes", "Avg bytes across partitions", LABELS)

        # File aggregate gauges
        self.files_min_record_count = Gauge("iceberg_files_min_record_count", "Min records per active data file", LABELS)
        self.files_max_record_count = Gauge("iceberg_files_max_record_count", "Max records per active data file", LABELS)
        self.files_avg_record_count = Gauge("iceberg_files_avg_record_count", "Avg records per active data file", LABELS)
        self.files_min_size_bytes = Gauge("iceberg_files_min_size_bytes", "Min size of active data files in bytes", LABELS)
        self.files_max_size_bytes = Gauge("iceberg_files_max_size_bytes", "Max size of active data files in bytes", LABELS)
        self.files_avg_size_bytes = Gauge("iceberg_files_avg_size_bytes", "Avg size of active data files in bytes", LABELS)
        self.files_small_file_count = Gauge(
            "iceberg_files_small_file_count",
            "Count of active data files below threshold_bytes",
            THRESHOLD_LABELS,
        )
        self.files_large_file_count = Gauge(
            "iceberg_files_large_file_count",
            "Count of active data files above threshold_bytes",
            THRESHOLD_LABELS,
        )
        self.files_avg_rows_per_file = Gauge(
            "iceberg_files_avg_rows_per_file",
            "Average rows per active data file",
            LABELS,
        )
        self.files_delete_file_ratio = Gauge(
            "iceberg_files_delete_file_ratio",
            "Delete files / data files ratio",
            LABELS,
        )

        # Rewrite / compaction inferred metrics
        self.rewrite_data_files_total = Counter(
            "iceberg_rewrite_data_files_total",
            "Estimated rewritten data files across rewrite-like snapshots",
            LABELS,
        )
        self.rewrite_bytes_total = Counter(
            "iceberg_rewrite_bytes_total",
            "Estimated rewritten bytes across rewrite-like snapshots",
            LABELS,
        )
        self.rewrite_snapshots_total = Counter(
            "iceberg_rewrite_snapshots_total",
            "Observed rewrite-like snapshots",
            LABELS,
        )
        self.rewrite_last_timestamp_seconds = Gauge(
            "iceberg_rewrite_last_timestamp_seconds",
            "Unix timestamp of latest rewrite-like snapshot",
            LABELS,
        )
        self.rewrite_removed_small_files_total = Counter(
            "iceberg_rewrite_removed_small_files_total",
            "Estimated small files removed by rewrite-like snapshots",
            LABELS,
        )
        self.rewrite_post_avg_file_size_bytes = Gauge(
            "iceberg_rewrite_post_avg_file_size_bytes",
            "Average file size after latest rewrite-like snapshot",
            LABELS,
        )
        self.table_compaction_candidate = Gauge(
            "iceberg_table_compaction_candidate",
            "Whether table is a compaction candidate (1/0)",
            LABELS,
        )


# ============================================================
# In-memory state for observed snapshots (counter correctness)
# ============================================================

class SnapshotState:
    """
    Tracks last seen snapshot ids to avoid double-incrementing counters.
    In Kubernetes this resets on pod restart, which is acceptable for Prometheus counters.
    Use increase()/rate() in PromQL.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen_snapshot_ids: Dict[Tuple[str, str, str], set[int]] = {}

    def mark_if_new(self, catalog: str, namespace: str, table: str, snapshot_id: int) -> bool:
        key = (catalog, namespace, table)
        with self._lock:
            seen = self._seen_snapshot_ids.setdefault(key, set())
            if snapshot_id in seen:
                return False
            seen.add(snapshot_id)
            # prevent unbounded growth
            if len(seen) > 5000:
                # retain latest-ish by arbitrary truncation strategy
                seen_list = list(seen)
                seen.clear()
                seen.update(seen_list[-1000:])
            return True


# ============================================================
# Helpers
# ============================================================

def safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def now_ts() -> int:
    return int(time.time())


def labels(catalog: str, namespace: str, table: str) -> Tuple[str, str, str, str]:
    return (catalog, namespace, table, f"{namespace}.{table}")


def avg(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def min_or_zero(values: List[float]) -> float:
    return min(values) if values else 0.0


def max_or_zero(values: List[float]) -> float:
    return max(values) if values else 0.0


def normalize_operation(op: Optional[str]) -> str:
    if not op:
        return "unknown"
    op = str(op).strip().lower().replace("-", "_")
    allowed = {"append", "overwrite", "replace", "delete", "rewrite", "fast_append"}
    return op if op in allowed else "unknown"


# ============================================================
# PyIceberg adapter layer (version-tolerant-ish)
# ============================================================

class IcebergAdapter:
    def __init__(self, cfg: CatalogConfig):
        props = {
            "uri": cfg.uri,
            **cfg.extra_properties,
        }
        if cfg.warehouse:
            props["warehouse"] = cfg.warehouse
        if cfg.token:
            props["token"] = cfg.token
        if cfg.credential:
            props["credential"] = cfg.credential

        self.catalog = load_catalog(cfg.name, **props)
        self.name = cfg.name

    def list_tables(self, namespace: str) -> List[TableRef]:
        # Adjust for your PyIceberg version if needed.
        # Some versions return tuples like (namespace_tuple, table_name) or identifiers.
        out: List[TableRef] = []
        tables = self.catalog.list_tables(namespace)
        for item in tables:
            if isinstance(item, tuple):
                # common shape: (('ns',), 'table') or ('ns', 'table')
                if len(item) == 2 and isinstance(item[1], str):
                    t = item[1]
                    out.append(TableRef(namespace=namespace, table=t))
                else:
                    out.append(TableRef(namespace=namespace, table=str(item[-1])))
            else:
                s = str(item)
                t = s.split(".")[-1]
                out.append(TableRef(namespace=namespace, table=t))
        return out

    def load_table(self, table_ref: TableRef):
        return self.catalog.load_table(table_ref.fqdn)


# ============================================================
# Data extraction contracts
# ============================================================

@dataclass
class SnapshotSummary:
    snapshot_id: int
    timestamp_ms: int
    sequence_number: int
    operation: str
    summary: Dict[str, Any]


@dataclass
class PartitionStats:
    partition_key: str
    record_count: int
    file_count: int
    size_bytes: int


@dataclass
class DeepScanStats:
    partition_stats: List[PartitionStats]
    file_record_counts: List[int]
    file_sizes: List[int]
    active_partition_count: int


# ============================================================
# Extractors
# ============================================================

class TableExtractor:
    """
    Isolates PyIceberg table metadata traversal.
    You WILL likely need to adapt some methods to your exact version.
    """

    def get_snapshots(self, table) -> List[SnapshotSummary]:
        snapshots = []

        # Version-flexible access
        metadata = getattr(table, "metadata", None)
        raw_snapshots = []
        if metadata is not None:
            raw_snapshots = getattr(metadata, "snapshots", None) or []

        for s in raw_snapshots:
            snapshot_id = safe_int(getattr(s, "snapshot_id", 0))
            timestamp_ms = safe_int(getattr(s, "timestamp_ms", 0))
            sequence_number = safe_int(getattr(s, "sequence_number", 0))
            summary = getattr(s, "summary", None) or {}
            operation = normalize_operation(summary.get("operation") or getattr(s, "operation", None))
            snapshots.append(
                SnapshotSummary(
                    snapshot_id=snapshot_id,
                    timestamp_ms=timestamp_ms,
                    sequence_number=sequence_number,
                    operation=operation,
                    summary=summary,
                )
            )

        snapshots.sort(key=lambda x: (x.sequence_number, x.timestamp_ms, x.snapshot_id))
        return snapshots

    def get_current_snapshot(self, table) -> Optional[SnapshotSummary]:
        snaps = self.get_snapshots(table)
        return snaps[-1] if snaps else None

    def get_current_totals_from_summary(self, current: SnapshotSummary) -> Dict[str, int]:
        s = current.summary or {}
        return {
            "total_data_files": safe_int(s.get("total-data-files")),
            "total_delete_files": safe_int(s.get("total-delete-files")),
            "total_records": safe_int(s.get("total-records")),
            "total_file_size_bytes": safe_int(s.get("total-files-size")),
            "total_position_delete_records": safe_int(s.get("total-position-deletes")),
            "total_equality_delete_records": safe_int(s.get("total-equality-deletes")),
        }

    def get_snapshot_delta_from_summary(self, snap: SnapshotSummary) -> Dict[str, int]:
        s = snap.summary or {}
        return {
            "added_records": safe_int(s.get("added-records")),
            "added_data_files": safe_int(s.get("added-data-files")),
            "added_delete_files": safe_int(s.get("added-delete-files")),
            "added_files_size_bytes": safe_int(s.get("added-files-size")),
            "removed_records": safe_int(s.get("removed-records")),
            "removed_data_files": safe_int(s.get("removed-data-files")),
            "removed_delete_files": safe_int(s.get("removed-delete-files")),
            "removed_files_size_bytes": safe_int(s.get("removed-files-size")),
            "changed_partition_count": safe_int(s.get("changed-partition-count")),
        }

    def deep_scan(self, table, max_partitions_per_table: int) -> DeepScanStats:
        """
        IMPORTANT: This is the one part you will almost certainly tweak.

        Ideal approach:
        - traverse current snapshot manifests / entries
        - include only ACTIVE data files
        - aggregate by partition

        Skeleton below tries a few common patterns and degrades gracefully.
        """
        partition_map: Dict[str, PartitionStats] = {}
        file_record_counts: List[int] = []
        file_sizes: List[int] = []

        # The actual PyIceberg APIs vary. Replace this section with your exact version's scan.
        # If you already know how to iterate current data files, plug it in here.
        current_files_iter = self._iter_current_data_files_best_effort(table)

        for data_file in current_files_iter:
            record_count = safe_int(getattr(data_file, "record_count", 0))
            file_size = safe_int(getattr(data_file, "file_size_in_bytes", 0))

            part = getattr(data_file, "partition", None)
            partition_key = self._partition_to_string(part)

            file_record_counts.append(record_count)
            file_sizes.append(file_size)

            if partition_key not in partition_map:
                if len(partition_map) >= max_partitions_per_table:
                    partition_key = "__overflow__"
                    if partition_key not in partition_map:
                        partition_map[partition_key] = PartitionStats(
                            partition_key=partition_key,
                            record_count=0,
                            file_count=0,
                            size_bytes=0,
                        )
                else:
                    partition_map[partition_key] = PartitionStats(
                        partition_key=partition_key,
                        record_count=0,
                        file_count=0,
                        size_bytes=0,
                    )

            ps = partition_map[partition_key]
            ps.record_count += record_count
            ps.file_count += 1
            ps.size_bytes += file_size

        return DeepScanStats(
            partition_stats=list(partition_map.values()),
            file_record_counts=file_record_counts,
            file_sizes=file_sizes,
            active_partition_count=len(partition_map),
        )

    def _iter_current_data_files_best_effort(self, table) -> Iterable[Any]:
        """
        Replace with exact PyIceberg method for your version.

        Possible strategies by version:
        - table.scan().plan_files() -> file scan tasks
        - metadata.current_snapshot + manifests traversal
        - table.inspect.files() (if available in your version)
        """
        # Strategy 1: scan().plan_files()
        try:
            scan = table.scan()
            tasks = scan.plan_files()
            for task in tasks:
                # common patterns: task.file or task.data_file
                df = getattr(task, "file", None) or getattr(task, "data_file", None)
                if df is not None:
                    yield df
            return
        except Exception:
            pass

        # Strategy 2: no-op fallback
        logger.warning("Deep scan fallback: unable to iterate current data files for table; returning empty deep stats")
        return []

    def _partition_to_string(self, part: Any) -> str:
        if part is None:
            return "__unpartitioned__"
        try:
            if hasattr(part, "__dict__"):
                items = sorted(part.__dict__.items())
                return ",".join(f"{k}={v}" for k, v in items)
            if isinstance(part, dict):
                items = sorted(part.items())
                return ",".join(f"{k}={v}" for k, v in items)
            return str(part)
        except Exception:
            return "__unknown_partition__"


# ============================================================
# Exporter core
# ============================================================

class IcebergExporter:
    def __init__(self, cfg: ExporterConfig):
        self.cfg = cfg
        self.metrics = Metrics()
        self.snapshot_state = SnapshotState()
        self.extractor = TableExtractor()
        self.stop_event = threading.Event()

        self.adapters = [IcebergAdapter(c) for c in cfg.catalogs]

        # cache for expensive deep scans
        self._deep_cache: Dict[Tuple[str, str, str], Tuple[float, DeepScanStats]] = {}
        self._deep_cache_lock = threading.Lock()

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            started = time.time()
            success = 1
            try:
                self.collect_once()
            except Exception:
                logger.exception("Exporter loop failed")
                success = 0
            finally:
                self.metrics.exporter_last_run_success.set(success)
                self.metrics.exporter_last_run_timestamp.set(now_ts())
                self.metrics.exporter_loop_duration.set(time.time() - started)

            self.stop_event.wait(self.cfg.scrape_interval_seconds)

    def stop(self) -> None:
        self.stop_event.set()

    def collect_once(self) -> None:
        for adapter in self.adapters:
            tables = self._discover_tables(adapter)
            for table_ref in tables:
                try:
                    self._collect_table(adapter, table_ref)
                except Exception:
                    logger.exception("Failed to collect table %s.%s", adapter.name, table_ref.fqdn)
                    self.metrics.exporter_table_scrape_errors.labels(
                        adapter.name, table_ref.namespace, table_ref.table, table_ref.fqdn, "collect"
                    ).inc()

    def _discover_tables(self, adapter: IcebergAdapter) -> List[TableRef]:
        d = self.cfg.discovery
        if d.mode == "explicit":
            return d.tables
        if d.mode == "namespace_scan":
            out: List[TableRef] = []
            for ns in d.namespaces:
                try:
                    out.extend(adapter.list_tables(ns))
                except Exception:
                    logger.exception("Failed listing namespace %s in catalog %s", ns, adapter.name)
            return out
        raise ValueError(f"Unsupported discovery mode: {d.mode}")

    def _collect_table(self, adapter: IcebergAdapter, table_ref: TableRef) -> None:
        table = adapter.load_table(table_ref)
        lbl = labels(adapter.name, table_ref.namespace, table_ref.table)

        snapshots = self.extractor.get_snapshots(table)
        current = snapshots[-1] if snapshots else None
        if current is None:
            logger.warning("No snapshots for %s.%s", adapter.name, table_ref.fqdn)
            return

        # 1) current snapshot gauges
        totals = self.extractor.get_current_totals_from_summary(current)
        self.metrics.snapshot_total_data_files.labels(*lbl).set(totals["total_data_files"])
        self.metrics.snapshot_total_delete_files.labels(*lbl).set(totals["total_delete_files"])
        self.metrics.snapshot_total_records.labels(*lbl).set(totals["total_records"])
        self.metrics.snapshot_total_file_size_bytes.labels(*lbl).set(totals["total_file_size_bytes"])
        self.metrics.snapshot_total_position_delete_records.labels(*lbl).set(totals["total_position_delete_records"])
        self.metrics.snapshot_total_equality_delete_records.labels(*lbl).set(totals["total_equality_delete_records"])
        self.metrics.table_current_snapshot_id.labels(*lbl).set(current.snapshot_id)
        self.metrics.table_current_sequence_number.labels(*lbl).set(current.sequence_number)
        self.metrics.table_snapshot_age_seconds.labels(*lbl).set(max(0, now_ts() - (current.timestamp_ms // 1000)))
        self.metrics.table_snapshot_count.labels(*lbl).set(len(snapshots))

        # 2) increment counters only for unseen snapshots
        for snap in snapshots:
            if not self.snapshot_state.mark_if_new(adapter.name, table_ref.namespace, table_ref.table, snap.snapshot_id):
                continue

            delta = self.extractor.get_snapshot_delta_from_summary(snap)
            self.metrics.snapshot_added_records_total.labels(*lbl).inc(delta["added_records"])
            self.metrics.snapshot_added_data_files_total.labels(*lbl).inc(delta["added_data_files"])
            self.metrics.snapshot_added_delete_files_total.labels(*lbl).inc(delta["added_delete_files"])
            self.metrics.snapshot_added_files_size_bytes_total.labels(*lbl).inc(delta["added_files_size_bytes"])
            self.metrics.snapshot_removed_records_total.labels(*lbl).inc(delta["removed_records"])
            self.metrics.snapshot_removed_data_files_total.labels(*lbl).inc(delta["removed_data_files"])
            self.metrics.snapshot_removed_delete_files_total.labels(*lbl).inc(delta["removed_delete_files"])
            self.metrics.snapshot_removed_files_size_bytes_total.labels(*lbl).inc(delta["removed_files_size_bytes"])
            self.metrics.snapshot_changed_partition_count.labels(*lbl).set(delta["changed_partition_count"])
            self.metrics.snapshot_commits_total.labels(*lbl, snap.operation).inc()

            # rewrite inference (summary-only heuristic)
            self._maybe_record_rewrite(lbl, snap, delta, totals)

        # 3) deep scan (cached)
        deep = self._get_or_refresh_deep_scan(adapter, table_ref, table)
        self._publish_deep_metrics(lbl, deep, totals)

    def _get_or_refresh_deep_scan(self, adapter: IcebergAdapter, table_ref: TableRef, table) -> DeepScanStats:
        key = (adapter.name, table_ref.namespace, table_ref.table)
        now = time.time()

        with self._deep_cache_lock:
            cached = self._deep_cache.get(key)
            if cached and (now - cached[0]) < self.cfg.deep_scan_interval_seconds:
                return cached[1]

        deep = self.extractor.deep_scan(table, self.cfg.max_partitions_per_table)

        with self._deep_cache_lock:
            self._deep_cache[key] = (now, deep)

        return deep

    def _publish_deep_metrics(self, lbl: Tuple[str, str, str, str], deep: DeepScanStats, totals: Dict[str, int]) -> None:
        # partition count
        self.metrics.table_partition_count.labels(*lbl).set(deep.active_partition_count)

        # per-partition gauges
        for p in deep.partition_stats:
            pl = (*lbl, p.partition_key)
            self.metrics.partition_record_count.labels(*pl).set(p.record_count)
            self.metrics.partition_file_count.labels(*pl).set(p.file_count)
            self.metrics.partition_size_bytes.labels(*pl).set(p.size_bytes)

        # partition aggregates
        part_records = [p.record_count for p in deep.partition_stats]
        part_files = [p.file_count for p in deep.partition_stats]
        part_sizes = [p.size_bytes for p in deep.partition_stats]

        self.metrics.partitions_min_record_count.labels(*lbl).set(min_or_zero(part_records))
        self.metrics.partitions_max_record_count.labels(*lbl).set(max_or_zero(part_records))
        self.metrics.partitions_avg_record_count.labels(*lbl).set(avg(part_records))
        self.metrics.partitions_min_file_count.labels(*lbl).set(min_or_zero(part_files))
        self.metrics.partitions_max_file_count.labels(*lbl).set(max_or_zero(part_files))
        self.metrics.partitions_avg_file_count.labels(*lbl).set(avg(part_files))
        self.metrics.partitions_min_size_bytes.labels(*lbl).set(min_or_zero(part_sizes))
        self.metrics.partitions_max_size_bytes.labels(*lbl).set(max_or_zero(part_sizes))
        self.metrics.partitions_avg_size_bytes.labels(*lbl).set(avg(part_sizes))

        # file aggregates
        fr = deep.file_record_counts
        fs = deep.file_sizes
        self.metrics.files_min_record_count.labels(*lbl).set(min_or_zero(fr))
        self.metrics.files_max_record_count.labels(*lbl).set(max_or_zero(fr))
        self.metrics.files_avg_record_count.labels(*lbl).set(avg(fr))
        self.metrics.files_min_size_bytes.labels(*lbl).set(min_or_zero(fs))
        self.metrics.files_max_size_bytes.labels(*lbl).set(max_or_zero(fs))
        self.metrics.files_avg_size_bytes.labels(*lbl).set(avg(fs))

        small = self.cfg.thresholds.small_file_bytes
        target = self.cfg.thresholds.target_file_bytes
        small_count = sum(1 for x in fs if x < small)
        large_count = sum(1 for x in fs if x > target)
        self.metrics.files_small_file_count.labels(*lbl, str(small)).set(small_count)
        self.metrics.files_large_file_count.labels(*lbl, str(target)).set(large_count)

        total_records = totals.get("total_records", 0)
        total_data_files = max(totals.get("total_data_files", 0), 1)
        total_delete_files = totals.get("total_delete_files", 0)

        self.metrics.files_avg_rows_per_file.labels(*lbl).set(total_records / total_data_files)
        self.metrics.files_delete_file_ratio.labels(*lbl).set(total_delete_files / total_data_files)

        # compaction candidate
        avg_file_size = avg(fs)
        avg_files_per_partition = avg(part_files)
        is_candidate = (
            (avg_file_size < self.cfg.thresholds.target_file_bytes if fs else False)
            or (small_count > self.cfg.thresholds.small_file_count)
            or ((total_delete_files / total_data_files) > self.cfg.thresholds.delete_file_ratio)
            or (avg_files_per_partition > self.cfg.thresholds.avg_files_per_partition if part_files else False)
        )
        self.metrics.table_compaction_candidate.labels(*lbl).set(1 if is_candidate else 0)

    def _maybe_record_rewrite(
        self,
        lbl: Tuple[str, str, str, str],
        snap: SnapshotSummary,
        delta: Dict[str, int],
        current_totals: Dict[str, int],
    ) -> None:
        operation = snap.operation
        added_records = delta["added_records"]
        removed_records = delta["removed_records"]
        added_data_files = delta["added_data_files"]
        removed_data_files = delta["removed_data_files"]
        removed_bytes = delta["removed_files_size_bytes"]

        total_records = max(current_totals.get("total_records", 0), 1)
        total_file_bytes = max(current_totals.get("total_file_size_bytes", 0), 1)
        total_data_files = max(current_totals.get("total_data_files", 0), 1)
        current_avg_file_size = total_file_bytes / total_data_files

        is_rewrite = (
            operation == "rewrite"
            or (
                removed_data_files > 0
                and added_data_files > 0
                and abs(added_records - removed_records) <= max(1000, int(total_records * 0.01))
            )
        )

        if not is_rewrite:
            return

        self.metrics.rewrite_data_files_total.labels(*lbl).inc(removed_data_files)
        self.metrics.rewrite_bytes_total.labels(*lbl).inc(removed_bytes)
        self.metrics.rewrite_snapshots_total.labels(*lbl).inc()
        self.metrics.rewrite_last_timestamp_seconds.labels(*lbl).set(snap.timestamp_ms // 1000)
        self.metrics.rewrite_post_avg_file_size_bytes.labels(*lbl).set(current_avg_file_size)

        # heuristic estimate for removed small files: if rewrite removed files and current avg > threshold, count all removed as "small-ish" removed
        if current_avg_file_size >= self.cfg.thresholds.target_file_bytes:
            self.metrics.rewrite_removed_small_files_total.labels(*lbl).inc(removed_data_files)


# ============================================================
# Config loading
# ============================================================

def load_config(path: str) -> ExporterConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    catalogs = []
    for c in raw.get("catalogs", []):
        extra = {k: v for k, v in c.items() if k not in {"name", "uri", "warehouse", "token", "credential"}}
        catalogs.append(
            CatalogConfig(
                name=c["name"],
                uri=c["uri"],
                warehouse=c.get("warehouse"),
                token=c.get("token"),
                credential=c.get("credential"),
                extra_properties=extra,
            )
        )

    discovery_raw = raw.get("discovery", {})
    tables = [TableRef(namespace=t["namespace"], table=t["table"]) for t in discovery_raw.get("tables", [])]
    discovery = DiscoveryConfig(
        mode=discovery_raw.get("mode", "explicit"),
        namespaces=discovery_raw.get("namespaces", []),
        tables=tables,
    )

    thresholds_raw = raw.get("thresholds", {})
    thresholds = ThresholdsConfig(
        small_file_bytes=thresholds_raw.get("small_file_bytes", 32 * 1024 * 1024),
        target_file_bytes=thresholds_raw.get("target_file_bytes", 128 * 1024 * 1024),
        delete_file_ratio=thresholds_raw.get("delete_file_ratio", 0.2),
        avg_files_per_partition=thresholds_raw.get("avg_files_per_partition", 20),
        small_file_count=thresholds_raw.get("small_file_count", 100),
    )

    return ExporterConfig(
        host=raw.get("host", "0.0.0.0"),
        port=raw.get("port", 9109),
        scrape_interval_seconds=raw.get("scrape_interval_seconds", 30),
        deep_scan_interval_seconds=raw.get("deep_scan_interval_seconds", 600),
        request_timeout_seconds=raw.get("request_timeout_seconds", 30),
        max_partitions_per_table=raw.get("max_partitions_per_table", 5000),
        catalogs=catalogs,
        discovery=discovery,
        thresholds=thresholds,
    )


# ============================================================
# Main
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="Iceberg REST Catalog Prometheus Exporter")
    parser.add_argument("--config", required=True, help="Path to config YAML")
    args = parser.parse_args()

    cfg = load_config(args.config)
    exporter = IcebergExporter(cfg)

    server, thread = start_http_server(cfg.port, addr=cfg.host)
    logger.info("Serving metrics on http://%s:%s/metrics", cfg.host, cfg.port)

    def handle_signal(signum, frame):
        logger.info("Received signal %s, shutting down", signum)
        exporter.stop()
        try:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        except Exception:
            logger.exception("Error shutting down metrics server")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    exporter.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

---

## Part 2 — Example config

### File: `config.yaml`

```yaml
host: 0.0.0.0
port: 9109
scrape_interval_seconds: 30
deep_scan_interval_seconds: 600
request_timeout_seconds: 30
max_partitions_per_table: 5000

catalogs:
  - name: prod
    uri: https://your-iceberg-rest-catalog.example.com
    warehouse: s3://warehouse
    token: ${ICEBERG_TOKEN}

# Choose one mode
# 1) explicit tables (recommended to start)
discovery:
  mode: explicit
  tables:
    - namespace: demo
      table: events
    - namespace: sales
      table: orders

# 2) namespace scan
# discovery:
#   mode: namespace_scan
#   namespaces:
#     - demo
#     - sales

thresholds:
  small_file_bytes: 33554432      # 32MB
  target_file_bytes: 134217728    # 128MB
  delete_file_ratio: 0.2
  avg_files_per_partition: 20
  small_file_count: 100
```

---

## Part 3 — Notes for your environment (important)

Given your scale (large Iceberg + real infra):

### What you should adjust first
1. Replace `TableExtractor._iter_current_data_files_best_effort()` with the **exact PyIceberg version-specific path** you use.
2. If manifest walking via `table.scan().plan_files()` is too expensive, implement a **direct manifest summary path**.
3. Keep `deep_scan_interval_seconds` at **300–1800s**. Do NOT do full deep scans every 30s.
4. Start with **explicit tables** first; then move to namespace discovery.

### Strong recommendation
If you want a truly production-grade version for your environment, the next step should be:
- split this into modules
- persist seen snapshots in a lightweight local file or PVC-backed state (optional)
- add `/healthz`
- add scrape concurrency limits (ThreadPool)
- add stale label cleanup for removed partitions

---

## Part 4 — Complete Grafana dashboard JSON

This dashboard assumes:
- Prometheus datasource variable = `${DS_PROMETHEUS}` (Grafana import prompt will map it)
- Variables:
  - `catalog`
  - `namespace`
  - `table`
- It maps the Datadog-style widgets to PromQL using the exporter metrics above.

### File: `iceberg_rest_dashboard.json`

```json
{
  "__inputs": [
    {
      "name": "DS_PROMETHEUS",
      "label": "Prometheus",
      "description": "",
      "type": "datasource",
      "pluginId": "prometheus",
      "pluginName": "Prometheus"
    }
  ],
  "__requires": [
    {
      "type": "grafana",
      "id": "grafana",
      "name": "Grafana",
      "version": "11.0.0"
    },
    {
      "type": "panel",
      "id": "stat",
      "name": "Stat",
      "version": ""
    },
    {
      "type": "panel",
      "id": "timeseries",
      "name": "Time series",
      "version": ""
    },
    {
      "type": "panel",
      "id": "table",
      "name": "Table",
      "version": ""
    },
    {
      "type": "panel",
      "id": "bargauge",
      "name": "Bar gauge",
      "version": ""
    }
  ],
  "annotations": {
    "list": [
      {
        "builtIn": 1,
        "datasource": {
          "type": "grafana",
          "uid": "-- Grafana --"
        },
        "enable": true,
        "hide": true,
        "iconColor": "rgba(0, 211, 255, 1)",
        "name": "Annotations & Alerts",
        "type": "dashboard"
      }
    ]
  },
  "editable": true,
  "fiscalYearStartMonth": 0,
  "graphTooltip": 1,
  "id": null,
  "links": [],
  "liveNow": false,
  "panels": [
    {
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "fieldConfig": {"defaults": {"unit": "short"}, "overrides": []},
      "gridPos": {"h": 4, "w": 6, "x": 0, "y": 0},
      "id": 1,
      "options": {"colorMode": "value", "graphMode": "area", "justifyMode": "auto", "orientation": "auto", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}, "textMode": "auto"},
      "targets": [{"expr": "iceberg_snapshot_total_records{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}", "refId": "A"}],
      "title": "Current Total Records",
      "type": "stat"
    },
    {
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "fieldConfig": {"defaults": {"unit": "decbytes"}, "overrides": []},
      "gridPos": {"h": 4, "w": 6, "x": 6, "y": 0},
      "id": 2,
      "options": {"colorMode": "value", "graphMode": "area", "justifyMode": "auto", "orientation": "auto", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}, "textMode": "auto"},
      "targets": [{"expr": "iceberg_snapshot_total_file_size_bytes{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}", "refId": "A"}],
      "title": "Current Total File Size",
      "type": "stat"
    },
    {
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "fieldConfig": {"defaults": {"unit": "short"}, "overrides": []},
      "gridPos": {"h": 4, "w": 6, "x": 12, "y": 0},
      "id": 3,
      "options": {"colorMode": "value", "graphMode": "area", "justifyMode": "auto", "orientation": "auto", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}, "textMode": "auto"},
      "targets": [{"expr": "iceberg_snapshot_total_data_files{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}", "refId": "A"}],
      "title": "Current Data Files",
      "type": "stat"
    },
    {
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "fieldConfig": {"defaults": {"unit": "short"}, "overrides": []},
      "gridPos": {"h": 4, "w": 6, "x": 18, "y": 0},
      "id": 4,
      "options": {"colorMode": "value", "graphMode": "area", "justifyMode": "auto", "orientation": "auto", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}, "textMode": "auto"},
      "targets": [{"expr": "iceberg_snapshot_total_delete_files{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}", "refId": "A"}],
      "title": "Current Delete Files",
      "type": "stat"
    },

    {
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "fieldConfig": {"defaults": {"unit": "s"}, "overrides": []},
      "gridPos": {"h": 4, "w": 6, "x": 0, "y": 4},
      "id": 5,
      "options": {"colorMode": "value", "graphMode": "none", "justifyMode": "auto", "orientation": "auto", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}, "textMode": "auto"},
      "targets": [{"expr": "iceberg_table_snapshot_age_seconds{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}", "refId": "A"}],
      "title": "Current Snapshot Age",
      "type": "stat"
    },
    {
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "fieldConfig": {"defaults": {"unit": "short"}, "overrides": []},
      "gridPos": {"h": 4, "w": 6, "x": 6, "y": 4},
      "id": 6,
      "options": {"colorMode": "value", "graphMode": "none", "justifyMode": "auto", "orientation": "auto", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}, "textMode": "auto"},
      "targets": [{"expr": "iceberg_table_partition_count{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}", "refId": "A"}],
      "title": "Active Partitions",
      "type": "stat"
    },
    {
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "fieldConfig": {"defaults": {"unit": "short"}, "overrides": []},
      "gridPos": {"h": 4, "w": 6, "x": 12, "y": 4},
      "id": 7,
      "options": {"colorMode": "background", "graphMode": "none", "justifyMode": "auto", "orientation": "auto", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}, "textMode": "auto"},
      "targets": [{"expr": "iceberg_table_compaction_candidate{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}", "refId": "A"}],
      "title": "Compaction Candidate (0/1)",
      "type": "stat"
    },
    {
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "fieldConfig": {"defaults": {"unit": "s"}, "overrides": []},
      "gridPos": {"h": 4, "w": 6, "x": 18, "y": 4},
      "id": 8,
      "options": {"colorMode": "value", "graphMode": "none", "justifyMode": "auto", "orientation": "auto", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}, "textMode": "auto"},
      "targets": [{"expr": "time() - iceberg_rewrite_last_timestamp_seconds{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}", "refId": "A"}],
      "title": "Time Since Last Rewrite",
      "type": "stat"
    },

    {
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "fieldConfig": {"defaults": {"unit": "short"}, "overrides": []},
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 8},
      "id": 9,
      "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": true}, "tooltip": {"mode": "single", "sort": "none"}},
      "targets": [
        {"expr": "increase(iceberg_snapshot_added_records_total{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}[$__rate_interval])", "legendFormat": "added", "refId": "A"},
        {"expr": "increase(iceberg_snapshot_removed_records_total{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}[$__rate_interval])", "legendFormat": "removed", "refId": "B"}
      ],
      "title": "Records Changed Over Time",
      "type": "timeseries"
    },
    {
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "fieldConfig": {"defaults": {"unit": "decbytes"}, "overrides": []},
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 8},
      "id": 10,
      "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": true}, "tooltip": {"mode": "single", "sort": "none"}},
      "targets": [
        {"expr": "increase(iceberg_snapshot_added_files_size_bytes_total{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}[$__rate_interval])", "legendFormat": "added bytes", "refId": "A"},
        {"expr": "increase(iceberg_snapshot_removed_files_size_bytes_total{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}[$__rate_interval])", "legendFormat": "removed bytes", "refId": "B"}
      ],
      "title": "Bytes Changed Over Time",
      "type": "timeseries"
    },

    {
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "fieldConfig": {"defaults": {"unit": "short"}, "overrides": []},
      "gridPos": {"h": 8, "w": 8, "x": 0, "y": 16},
      "id": 11,
      "options": {"displayMode": "gradient", "orientation": "horizontal", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}, "showUnfilled": true},
      "targets": [{"expr": "sum by (operation) (increase(iceberg_snapshot_commits_total{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}[$__range]))", "refId": "A"}],
      "title": "Snapshot Commits by Operation",
      "type": "bargauge"
    },
    {
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "fieldConfig": {"defaults": {"unit": "decbytes"}, "overrides": []},
      "gridPos": {"h": 8, "w": 8, "x": 8, "y": 16},
      "id": 12,
      "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": true}, "tooltip": {"mode": "single", "sort": "none"}},
      "targets": [
        {"expr": "increase(iceberg_rewrite_bytes_total{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}[$__rate_interval])", "legendFormat": "rewrite bytes", "refId": "A"}
      ],
      "title": "Bytes Rewritten (Datadog: bytes compacted replacement)",
      "type": "timeseries"
    },
    {
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "fieldConfig": {"defaults": {"unit": "short"}, "overrides": []},
      "gridPos": {"h": 8, "w": 8, "x": 16, "y": 16},
      "id": 13,
      "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": true}, "tooltip": {"mode": "single", "sort": "none"}},
      "targets": [
        {"expr": "increase(iceberg_rewrite_data_files_total{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}[$__rate_interval])", "legendFormat": "rewrite files", "refId": "A"}
      ],
      "title": "Files Rewritten (Datadog: files compacted replacement)",
      "type": "timeseries"
    },

    {
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "fieldConfig": {"defaults": {"unit": "decbytes"}, "overrides": []},
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 24},
      "id": 14,
      "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": true}, "tooltip": {"mode": "single", "sort": "none"}},
      "targets": [
        {"expr": "iceberg_files_avg_size_bytes{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}", "legendFormat": "avg", "refId": "A"},
        {"expr": "iceberg_files_min_size_bytes{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}", "legendFormat": "min", "refId": "B"},
        {"expr": "iceberg_files_max_size_bytes{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}", "legendFormat": "max", "refId": "C"}
      ],
      "title": "File Size Stats",
      "type": "timeseries"
    },
    {
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "fieldConfig": {"defaults": {"unit": "short"}, "overrides": []},
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 24},
      "id": 15,
      "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": true}, "tooltip": {"mode": "single", "sort": "none"}},
      "targets": [
        {"expr": "iceberg_files_avg_record_count{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}", "legendFormat": "avg", "refId": "A"},
        {"expr": "iceberg_files_min_record_count{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}", "legendFormat": "min", "refId": "B"},
        {"expr": "iceberg_files_max_record_count{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}", "legendFormat": "max", "refId": "C"}
      ],
      "title": "Records Per File Stats",
      "type": "timeseries"
    },

    {
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "fieldConfig": {"defaults": {"unit": "short"}, "overrides": []},
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 32},
      "id": 16,
      "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": true}, "tooltip": {"mode": "single", "sort": "none"}},
      "targets": [
        {"expr": "topk(10, iceberg_partition_record_count{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"})", "legendFormat": "{{partition}}", "refId": "A"}
      ],
      "title": "Top 10 Partitions by Records",
      "type": "timeseries"
    },
    {
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "fieldConfig": {"defaults": {"unit": "short"}, "overrides": []},
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 32},
      "id": 17,
      "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": true}, "tooltip": {"mode": "single", "sort": "none"}},
      "targets": [
        {"expr": "topk(10, iceberg_partition_file_count{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"})", "legendFormat": "{{partition}}", "refId": "A"}
      ],
      "title": "Top 10 Partitions by File Count",
      "type": "timeseries"
    },

    {
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "fieldConfig": {"defaults": {"unit": "short"}, "overrides": []},
      "gridPos": {"h": 8, "w": 8, "x": 0, "y": 40},
      "id": 18,
      "options": {"colorMode": "value", "graphMode": "none", "justifyMode": "auto", "orientation": "auto", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}, "textMode": "auto"},
      "targets": [{"expr": "iceberg_files_small_file_count{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\",threshold_bytes=\"33554432\"}", "refId": "A"}],
      "title": "Small Files (<32MB)",
      "type": "stat"
    },
    {
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "fieldConfig": {"defaults": {"unit": "percentunit"}, "overrides": []},
      "gridPos": {"h": 8, "w": 8, "x": 8, "y": 40},
      "id": 19,
      "options": {"colorMode": "value", "graphMode": "none", "justifyMode": "auto", "orientation": "auto", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}, "textMode": "auto"},
      "targets": [{"expr": "iceberg_files_delete_file_ratio{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}", "refId": "A"}],
      "title": "Delete File Ratio",
      "type": "stat"
    },
    {
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "fieldConfig": {"defaults": {"unit": "short"}, "overrides": []},
      "gridPos": {"h": 8, "w": 8, "x": 16, "y": 40},
      "id": 20,
      "options": {"colorMode": "value", "graphMode": "none", "justifyMode": "auto", "orientation": "auto", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}, "textMode": "auto"},
      "targets": [{"expr": "increase(iceberg_rewrite_snapshots_total{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}[$__range])", "refId": "A"}],
      "title": "Rewrite Events in Range",
      "type": "stat"
    },

    {
      "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
      "fieldConfig": {"defaults": {}, "overrides": []},
      "gridPos": {"h": 10, "w": 24, "x": 0, "y": 48},
      "id": 21,
      "options": {"showHeader": true},
      "targets": [
        {
          "expr": "iceberg_partition_size_bytes{catalog=~\"$catalog\",namespace=~\"$namespace\",table=~\"$table\"}",
          "format": "table",
          "instant": true,
          "refId": "A"
        }
      ],
      "title": "Partition Size Table",
      "transformations": [
        {"id": "labelsToFields", "options": {"mode": "columns"}},
        {"id": "organize", "options": {"excludeByName": {"Time": true}, "renameByName": {"Value": "size_bytes"}}}
      ],
      "type": "table"
    }
  ],
  "refresh": "30s",
  "schemaVersion": 39,
  "style": "dark",
  "tags": ["iceberg", "prometheus", "rest-catalog"],
  "templating": {
    "list": [
      {
        "current": {"selected": true, "text": [".*"], "value": [".*"]},
        "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
        "definition": "label_values(iceberg_snapshot_total_records, catalog)",
        "hide": 0,
        "includeAll": true,
        "label": "catalog",
        "multi": true,
        "name": "catalog",
        "options": [],
        "query": {"qryType": 1, "query": "label_values(iceberg_snapshot_total_records, catalog)", "refId": "Prometheus-catalog"},
        "refresh": 2,
        "regex": "",
        "sort": 1,
        "type": "query"
      },
      {
        "current": {"selected": true, "text": [".*"], "value": [".*"]},
        "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
        "definition": "label_values(iceberg_snapshot_total_records{catalog=~\"$catalog\"}, namespace)",
        "hide": 0,
        "includeAll": true,
        "label": "namespace",
        "multi": true,
        "name": "namespace",
        "options": [],
        "query": {"qryType": 1, "query": "label_values(iceberg_snapshot_total_records{catalog=~\"$catalog\"}, namespace)", "refId": "Prometheus-namespace"},
        "refresh": 2,
        "regex": "",
        "sort": 1,
        "type": "query"
      },
      {
        "current": {"selected": true, "text": [".*"], "value": [".*"]},
        "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
        "definition": "label_values(iceberg_snapshot_total_records{catalog=~\"$catalog\",namespace=~\"$namespace\"}, table)",
        "hide": 0,
        "includeAll": true,
        "label": "table",
        "multi": true,
        "name": "table",
        "options": [],
        "query": {"qryType": 1, "query": "label_values(iceberg_snapshot_total_records{catalog=~\"$catalog\",namespace=~\"$namespace\"}, table)", "refId": "Prometheus-table"},
        "refresh": 2,
        "regex": "",
        "sort": 1,
        "type": "query"
      }
    ]
  },
  "time": {"from": "now-24h", "to": "now"},
  "timepicker": {},
  "timezone": "browser",
  "title": "Iceberg REST Catalog Health",
  "uid": "iceberg-rest-catalog-health",
  "version": 1,
  "weekStart": ""
}
```

---

## Part 5 — Datadog widget → PromQL mapping reference

### Direct mappings
- **Current total records**
  - `iceberg_snapshot_total_records`
- **Current total file size**
  - `iceberg_snapshot_total_file_size_bytes`
- **Current total data files**
  - `iceberg_snapshot_total_data_files`
- **Current total delete files**
  - `iceberg_snapshot_total_delete_files`
- **Snapshot age**
  - `iceberg_table_snapshot_age_seconds`
- **Active partitions**
  - `iceberg_table_partition_count`

### Snapshot delta mappings
- **Added records over time**
  - `increase(iceberg_snapshot_added_records_total[$__rate_interval])`
- **Removed records over time**
  - `increase(iceberg_snapshot_removed_records_total[$__rate_interval])`
- **Added bytes over time**
  - `increase(iceberg_snapshot_added_files_size_bytes_total[$__rate_interval])`
- **Removed bytes over time**
  - `increase(iceberg_snapshot_removed_files_size_bytes_total[$__rate_interval])`

### Glue compaction replacements
- **Number of bytes compacted**
  - `increase(iceberg_rewrite_bytes_total[$__rate_interval])`
- **Number of files compacted**
  - `increase(iceberg_rewrite_data_files_total[$__rate_interval])`
- **Compaction events**
  - `increase(iceberg_rewrite_snapshots_total[$__range])`
- **Time since last compaction/rewrite**
  - `time() - iceberg_rewrite_last_timestamp_seconds`

---

## Part 6 — Best next step for you

For your real environment, the next best step is:

1. I can produce a **multi-file production version**:
   - `catalog_client.py`
   - `collectors/summary.py`
   - `collectors/deep_scan.py`
   - `collectors/rewrite.py`
   - `main.py`
   - `Dockerfile`
   - `Helm values.yaml`
   - `ServiceMonitor`

2. Or I can produce a **PyIceberg-version-specific implementation** if you tell me:
   - your exact `pyiceberg` version
   - your REST catalog implementation/vendor
   - whether `table.scan().plan_files()` works in your environment

That second option is the one I’d recommend for you, because the only non-trivial part here is the exact file/manifest traversal path.

