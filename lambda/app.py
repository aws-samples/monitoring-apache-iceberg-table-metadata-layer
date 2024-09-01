import boto3
import numpy as np
from datetime import datetime, timezone
from pyiceberg.catalog.glue import GlueCatalog
from pyiceberg.table import Table
from pyiceberg.table.snapshots import Snapshot
import os
import pyarrow.compute as pc
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

required_vars = ['CW_NAMESPACE']
for var in required_vars:
    # Retrieve the environment variable value
    if os.getenv(var) is None:
        # If any variable is not set, raise an exception
        raise EnvironmentError(f"Required environment variable '{var}' is not set.")
    
cw_namespace = os.environ.get('CW_NAMESPACE')

def send_custom_metric( metric_name, dimensions, value, unit, namespace, timestamp=None):
    """
    Send a custom metric to AWS CloudWatch.

    :param namespace: The namespace for the metric data.
    :param ts: The ts timestamp.
    :param metric_name: The name of the metric.
    :param dimensions: A list of dictionaries, each containing 'Name' and 'Value' keys for the metric dimensions.
    :param value: The value for the metric.
    :param unit: The unit of the metric.
    """
    cloudwatch = boto3.client('cloudwatch')

    metric_data = {
        'MetricName': metric_name,
        'Dimensions': dimensions,
        'Value': value,
        'Unit': unit
    }

    if timestamp:
        metric_data['Timestamp'] = datetime.fromtimestamp(timestamp / 1000.0, tz=timezone.utc)
    else:
        metric_data['Timestamp'] = datetime.now()

    cloudwatch.put_metric_data(
        Namespace=namespace,
        MetricData=[metric_data]
    )

def normalize_metrics(metrics: dict):
    return {k: v.item() if isinstance(v, np.int64) or isinstance(v, np.float64) 
            else v 
            for k, v in metrics.items()}

def send_metrics(metrics: dict, namespace: str, table: Table, snapshot: Snapshot):
    normalized_metrics = normalize_metrics(metrics)
    for metric_name, metric_value in normalized_metrics.items():
        logger.info(f"metric_name={namespace}.{metric_name}, metric_value={metric_value}")
        send_custom_metric(
            metric_name=f"{namespace}.{metric_name}",
            dimensions=[
                {'Name': 'table_name', 'Value': f"{table.name()[1]}.{table.name()[2]}"}
            ],
            value=metric_value,
            unit='Bytes' if "size" in metric_name else "Count",
            namespace=cw_namespace,
            timestamp = snapshot.timestamp_ms,
        )

def send_files_metrics(table: Table, snapshot: Snapshot):
    logger.info(f"send_files_metrics() -> snapshot_id={snapshot.snapshot_id}")
    df = table.inspect.files().to_pandas()
    file_metrics = {
        "avg_record_count": df["record_count"].astype(int).mean().astype(int),
        "max_record_count": df["record_count"].astype(int).max(),
        "min_record_count": df["record_count"].astype(int).min(),
        "avg_file_size": df['file_size_in_bytes'].astype(int).mean().astype(int),
        "max_file_size": df['file_size_in_bytes'].astype(int).max(),
        "min_file_size": df['file_size_in_bytes'].astype(int).min()
    }
    
    logger.info("file_metrics=")
    logger.info(file_metrics)
    send_metrics(file_metrics, "files", table, snapshot)
    

def send_partition_metrics(table: Table, snapshot: Snapshot):
    logger.info(f"send_partition_metrics() -> snapshot_id={snapshot.snapshot_id}")
    if not table.metadata.partition_specs:
        logger.info("No partitions found")
        return
    
    df = table.inspect.partitions().to_pandas()
    partition_metrics = {
        "avg_record_count": df["record_count"].astype(int).mean().astype(int),
        "max_record_count": df["record_count"].astype(int).max(),
        "min_record_count": df["record_count"].astype(int).min(),
        "deviation_record_count": df['record_count'].astype(int).std().round(2),
        "skew_record_count": df['record_count'].astype(int).skew().round(2),
        "avg_file_count": df['file_count'].astype(int).mean().astype(int),
        "max_file_count": df['file_count'].astype(int).max(),
        "min_file_count": df['file_count'].astype(int).min(),
        "deviation_file_count": df['file_count'].astype(int).std().round(2),
        "skew_file_count": df['file_count'].astype(int).skew().round(2), 
    }
    
    logger.info("partition_metrics=")
    logger.info(partition_metrics)
    
    send_metrics(partition_metrics, "partitions", table, snapshot)
    
    for index, row in df.iterrows():
        partition_name = "_".join(f"{val}" for val in row['partition'].values())
        record_count = row['record_count']
        file_count = row['file_count']
        logger.info(f"partition_name={partition_name}, record_count={record_count}, file_count={file_count}")
        
        send_custom_metric(
            metric_name=f"partitions.record_count",
            dimensions=[
                {'Name': 'table_name', 'Value': f"{table.name()[1]}.{table.name()[2]}"},
                {'Name': 'partition_name', 'Value': partition_name}
            ],
            value=int(record_count),
            unit='Count',
            namespace=cw_namespace,
            timestamp = snapshot.timestamp_ms,
        )
        
        send_custom_metric(
            metric_name=f"partitions.file_count",
            dimensions=[
                {'Name': 'table_name', 'Value': f"{table.name()[1]}.{table.name()[2]}"},
                {'Name': 'partition_name', 'Value': partition_name}
            ],
            value=int(file_count),
            unit='Count',
            namespace=cw_namespace,
            timestamp = snapshot.timestamp_ms,
        )
    
    return partition_metrics
    

def send_snapshot_metrics(table: Table, snapshot: Snapshot):
    logger.info("send_snapshot_metrics")
    snapshot_id = snapshot.snapshot_id
    logger.info(f"send_snapshot_metrics() -> snapshot_id={snapshot_id}")
    expr = pc.field("snapshot_id") == snapshot_id
    snapshots = table.inspect.snapshots().filter(expr).to_pylist()
    snapshot_metrics_obj = snapshots[0]
    snapshot_metrics = dict(snapshot_metrics_obj["summary"])
    metrics = [
        "added-data-files", "added-records", "changed-partition-count", 
        "total-records","total-data-files", "total-delete-files",
        "added-files-size", "total-files-size", "added-position-deletes"
    ]
    snapshot_metrics = { k.replace("-", "_"): int(snapshot_metrics.get(k, 0)) for k in metrics }
    
    logger.info("snapshot_metrics=")
    logger.info(snapshot_metrics)
    send_metrics(snapshot_metrics, "snapshots", table, snapshot)
     

# check if glue table is of iceberg format, return boolean
def check_table_is_of_iceberg_format(event):
    glue_client = boto3.client('glue')
    response = glue_client.get_table(
        DatabaseName=event["detail"]["databaseName"],
        Name=event["detail"]["tableName"],
    )
    try:
        return response["Table"]["Parameters"]["table_type"] == "ICEBERG"
    except KeyError:
        logger.warning("check_table_is_of_iceberg_format() -> table_type is missing")
        return False
    

def lambda_handler(event, context):
    log_format = f"[{context.aws_request_id}:%(message)s"
    logging.basicConfig(format=log_format, level=logging.INFO)
    
    # Ensure Table is of Iceberg format.
    if not check_table_is_of_iceberg_format(event):
        logger.info("Table is not of Iceberg format, skipping metrics generation")
        return
    
    glue_db_name = event["detail"]["databaseName"]
    glue_table_name =  event["detail"]["tableName"]
    
    catalog = GlueCatalog(glue_db_name)
    table = catalog.load_table((glue_db_name, glue_table_name))
    snapshot = table.current_snapshot()
    
    send_snapshot_metrics(table, snapshot)
    send_partition_metrics(table, snapshot)
    send_files_metrics(table, snapshot)