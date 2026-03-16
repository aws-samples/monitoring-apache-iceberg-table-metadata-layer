import os
import logging
import pyiceberg
from prometheus_client import start_http_server, Gauge
from pyiceberg.catalog import load_catalog

logger = logging.getLogger(__name__)

# Define Snapshot Metrics
SNAPSHOT_METRICS = {
    'added_data_files': Gauge('iceberg_snapshot_added_data_files', 'Added data files', ['table_name']),
    'added_records': Gauge('iceberg_snapshot_added_records', 'Added records', ['table_name']),
    'total_records': Gauge('iceberg_snapshot_total_records', 'Total records', ['table_name']),
    'total_data_files': Gauge('iceberg_snapshot_total_data_files', 'Total data files', ['table_name']),
    'total_delete_files': Gauge('iceberg_snapshot_total_delete_files', 'Total delete files', ['table_name']),
    'added_files_size': Gauge('iceberg_snapshot_added_files_size', 'Added files size', ['table_name']),
    'total_files_size': Gauge('iceberg_snapshot_total_files_size', 'Total files size', ['table_name']),
    'added_position_deletes': Gauge('iceberg_snapshot_added_position_deletes', 'Added position deletes', ['table_name']),
    'changed_partition_count': Gauge('iceberg_snapshot_changed_partition_count', 'Changed partition count', ['table_name']),
}

# Define Maintenance Metrics (mapped from summary or simulated for demo)
MAINTENANCE_METRICS = {
    'compacted_data_files': Gauge('iceberg_maintenance_compacted_data_files', 'Number of files compacted', ['table_name']),
    'compacted_files_size': Gauge('iceberg_maintenance_compacted_files_size', 'Size of files compacted', ['table_name']),
}

# Define File Metrics
FILE_METRICS = {
    'avg_record_count': Gauge('iceberg_files_avg_record_count', 'Avg record count in files', ['table_name']),
    'max_record_count': Gauge('iceberg_files_max_record_count', 'Max record count in files', ['table_name']),
    'min_record_count': Gauge('iceberg_files_min_record_count', 'Min record count in files', ['table_name']),
    'avg_file_size': Gauge('iceberg_files_avg_file_size', 'Avg file size', ['table_name']),
    'max_file_size': Gauge('iceberg_files_max_file_size', 'Max file size', ['table_name']),
    'min_file_size': Gauge('iceberg_files_min_file_size', 'Min file size', ['table_name']),
}

# Define Partition Metrics
PARTITION_SUMMARY_METRICS = {
    'min_record_count': Gauge('iceberg_partitions_min_record_count', 'Min record count in partitions', ['table_name']),
    'max_record_count': Gauge('iceberg_partitions_max_record_count', 'Max record count in partitions', ['table_name']),
    'avg_record_count': Gauge('iceberg_partitions_avg_record_count', 'Avg record count in partitions', ['table_name']),
    'min_file_count': Gauge('iceberg_partitions_min_file_count', 'Min file count in partitions', ['table_name']),
    'max_file_count': Gauge('iceberg_partitions_max_file_count', 'Max file count in partitions', ['table_name']),
    'avg_file_count': Gauge('iceberg_partitions_avg_file_count', 'Avg file count in partitions', ['table_name']),
}

PARTITION_DETAIL_METRICS = {
    'record_count': Gauge('iceberg_partition_record_count', 'Record count per partition', ['table_name', 'partition_name']),
    'file_count': Gauge('iceberg_partition_file_count', 'File count per partition', ['table_name', 'partition_name']),
}

# Track state between polls to make maintenance metrics cumulative
SNAPSHOT_STATE = {} # table_name -> last_seen_snapshot_id
MAINTENANCE_TOTALS = {
    'compacted_data_files': {}, # table_name -> total_count
    'compacted_files_size': {}, # table_name -> total_size
}

def safe_float(val, default=0.0):
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default

def start_server(port):
    start_http_server(port)
    logger.info(f"Prometheus metrics server started on port {port}")
    logger.info(f"PyIceberg version: {pyiceberg.__version__}")

def update_metrics():
    catalog_uri = os.getenv('CATALOG_URI', 'http://localhost:8181/')
    s3_endpoint = os.getenv('S3_ENDPOINT', 'http://localhost:9000')
    aws_access_key = os.getenv('AWS_ACCESS_KEY_ID', 'minio')
    aws_secret_key = os.getenv('AWS_SECRET_ACCESS_KEY', 'minio123')
    
    try:
        catalog = load_catalog(
            "default",
            **{
                "type": "rest",
                "uri": catalog_uri,
                "s3.endpoint": s3_endpoint,
                "s3.access-key-id": aws_access_key,
                "s3.secret-access-key": aws_secret_key,
                "s3.path-style-access": "true"
            }
        )
        
        namespaces = catalog.list_namespaces()
        logger.info(f"Found namespaces: {namespaces}")
        
        for namespace_tuple in namespaces:
            namespace = namespace_tuple[0] if isinstance(namespace_tuple, tuple) else namespace_tuple
            if namespace in ['system', 'information_schema']:
                continue
                
            tables = catalog.list_tables(namespace)
            logger.info(f"Found {len(tables)} tables in namespace {namespace}")
            
            for table_identifier in tables:
                if isinstance(table_identifier, tuple):
                    table_name = ".".join(table_identifier)
                else:
                    table_name = table_identifier
                
                logger.info(f"Processing table {table_name}")
                
                # Initialize state for new tables
                if table_name not in MAINTENANCE_TOTALS['compacted_data_files']:
                    MAINTENANCE_TOTALS['compacted_data_files'][table_name] = 0.0
                    MAINTENANCE_TOTALS['compacted_files_size'][table_name] = 0.0
                    MAINTENANCE_METRICS['compacted_data_files'].labels(table_name=table_name).set(0)
                    MAINTENANCE_METRICS['compacted_files_size'].labels(table_name=table_name).set(0)

                # Initialize Snapshot Gauges to 0 (current state)
                for gauge in SNAPSHOT_METRICS.values():
                    gauge.labels(table_name=table_name).set(0)
                
                # Explicitly reload to get latest metadata
                table = catalog.load_table(table_identifier)
                
                # Snapshot metrics
                try:
                    snapshot = table.current_snapshot()
                    if snapshot:
                        summary = snapshot.summary
                        op_str = str(summary.get('operation', 'unknown')).lower()
                        
                        # Only process maintenance if it's a NEW snapshot
                        last_id = SNAPSHOT_STATE.get(table_name)
                        if last_id != snapshot.snapshot_id:
                            logger.info(f"New snapshot detected for {table_name}: {snapshot.snapshot_id} (Op: {op_str})")
                            
                            # Increment maintenance totals if applicable
                            compact_files = safe_float(summary.get('removed-data-files') or summary.get('deleted-data-files') or 0)
                            
                            is_maint = 'replace' in op_str or 'overwrite' in op_str or 'delete' in op_str
                            size = 0.0
                            if is_maint:
                                size = safe_float(summary.get('removed-files-size') or summary.get('deleted-files-size') or summary.get('added-files-size') or 0)
                            
                            if compact_files > 0 or size > 0:
                                MAINTENANCE_TOTALS['compacted_data_files'][table_name] += compact_files
                                MAINTENANCE_TOTALS['compacted_files_size'][table_name] += size
                                logger.info(f"UPDATED MAINTENANCE TOTALS for {table_name}: +{compact_files} files, +{size} bytes")
                            
                            SNAPSHOT_STATE[table_name] = snapshot.snapshot_id
                        
                        # Always set current snapshot metrics
                        for metric_key, gauge in SNAPSHOT_METRICS.items():
                            summary_key = metric_key.replace('_', '-')
                            val = summary.get(summary_key, 0)
                            gauge.labels(table_name=table_name).set(safe_float(val))
                        
                        # Always set the cumulative maintenance gauges
                        MAINTENANCE_METRICS['compacted_data_files'].labels(table_name=table_name).set(MAINTENANCE_TOTALS['compacted_data_files'][table_name])
                        MAINTENANCE_METRICS['compacted_files_size'].labels(table_name=table_name).set(MAINTENANCE_TOTALS['compacted_files_size'][table_name])
                    else:
                        logger.info(f"No snapshots found for {table_name}")
                except Exception as e:
                    logger.error(f"Error updating snapshot metrics for {table_name}: {e}")
                        
                # File and Partition metrics via pyiceberg inspect
                try:
                    if hasattr(table, 'inspect'):
                        # Files
                        files_df = table.inspect.files().to_pandas()
                        if not files_df.empty:
                            logger.info(f"Updating file metrics for {table_name}")
                            FILE_METRICS['avg_record_count'].labels(table_name=table_name).set(safe_float(files_df['record_count'].mean()))
                            FILE_METRICS['max_record_count'].labels(table_name=table_name).set(safe_float(files_df['record_count'].max()))
                            FILE_METRICS['min_record_count'].labels(table_name=table_name).set(safe_float(files_df['record_count'].min()))
                            FILE_METRICS['avg_file_size'].labels(table_name=table_name).set(safe_float(files_df['file_size_in_bytes'].mean()))
                            FILE_METRICS['max_file_size'].labels(table_name=table_name).set(safe_float(files_df['file_size_in_bytes'].max()))
                            FILE_METRICS['min_file_size'].labels(table_name=table_name).set(safe_float(files_df['file_size_in_bytes'].min()))
                        
                        # Partitions
                        partitions_df = table.inspect.partitions().to_pandas()
                        if not partitions_df.empty:
                            logger.info(f"Updating partition metrics for {table_name}")
                            PARTITION_SUMMARY_METRICS['avg_record_count'].labels(table_name=table_name).set(safe_float(partitions_df['record_count'].mean()))
                            PARTITION_SUMMARY_METRICS['max_record_count'].labels(table_name=table_name).set(safe_float(partitions_df['record_count'].max()))
                            PARTITION_SUMMARY_METRICS['min_record_count'].labels(table_name=table_name).set(safe_float(partitions_df['record_count'].min()))
                            PARTITION_SUMMARY_METRICS['avg_file_count'].labels(table_name=table_name).set(safe_float(partitions_df['file_count'].mean()))
                            PARTITION_SUMMARY_METRICS['max_file_count'].labels(table_name=table_name).set(safe_float(partitions_df['file_count'].max()))
                            PARTITION_SUMMARY_METRICS['min_file_count'].labels(table_name=table_name).set(safe_float(partitions_df['file_count'].min()))
                            
                            # Detailed partition metrics (limited to avoid too many series)
                            for _, row in partitions_df.sort_values('record_count', ascending=False).head(20).iterrows():
                                p_name = str(row['partition'])
                                PARTITION_DETAIL_METRICS['record_count'].labels(table_name=table_name, partition_name=p_name).set(safe_float(row['record_count']))
                                PARTITION_DETAIL_METRICS['file_count'].labels(table_name=table_name, partition_name=p_name).set(safe_float(row['file_count']))
                except Exception as e:
                    logger.error(f"Could not inspect table {table_name}: {e}")
                    
    except Exception as e:
        logger.error(f"Error in update_metrics: {e}")
