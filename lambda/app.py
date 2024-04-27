import json
import boto3
from datetime import datetime, timezone
from pyiceberg.catalog.glue import GlueCatalog
import os
import time
import uuid
import pandas as pd
import numpy as np
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

glue_client = boto3.client('glue')

required_vars = ['DBNAME', 'TABLENAME', 'CW_NAMESPACE', 'GLUE_SERVICE_ROLE', 'SPARK_CATALOG_S3_WAREHOUSE']
for var in required_vars:
    # Retrieve the environment variable value
    if os.getenv(var) is None:
        # If any variable is not set, raise an exception
        raise EnvironmentError(f"Required environment variable '{var}' is not set.")
    
cw_namespace = os.environ.get('CW_NAMESPACE')
glue_db_name = os.environ.get('DBNAME')
glue_table_name = os.environ.get('TABLENAME')
glue_service_role = os.environ.get('GLUE_SERVICE_ROLE')
warehouse_path = os.environ.get('SPARK_CATALOG_S3_WAREHOUSE')

glue_session_tags = {
    "app": "iceberg-monitoring"
}

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
    
def wait_for_session(session_id,interval=1):
    while True:
        response = glue_client.get_session(
            Id=session_id
        )
        status = response["Session"]["Status"]
        if status in ['READY','FAILED','TIMEOUT','STOPPED']:
            logger.info(f"Session {session_id} status={status}")
            break
        time.sleep(interval)
        
def wait_for_statement(session_id,statement_id,interval=1):
    while True:
        response = glue_client.get_statement(
            SessionId=session_id,
            Id=statement_id,
        )
        status = response["Statement"]["State"]
        if status in ['AVAILABLE','CANCELLED','ERROR']:
            logger.info(f"Statement status={status}")
            return response
        time.sleep(interval)
        
def parse_statement_result(data_str, columns):
    # Split the string into lines and filter out the irrelevant ones
    lines = data_str.split('\n')[3:-3]  # Ignore the header and footer lines
    # Split each line into components and strip the whitespace
    data = [line.split('|')[1:-1] for line in lines]  # Remove empty strings at start and end
    data = [[item.strip() for item in row] for row in data]  # Strip whitespace from each item
    df = pd.DataFrame(data, columns=columns)
    return df
        

def send_files_metrics(snapshot,session_id):
    sql_stmt = f"select file_path,record_count,file_size_in_bytes from glue_catalog.{glue_db_name}.{glue_table_name}.files"    
    run_stmt_response = glue_client.run_statement(
        SessionId=session_id,
        Code=f"df = spark.sql(\"{sql_stmt}\");df.show(df.count(),truncate=False)"
    )
    stmt_id = run_stmt_response["Id"]
    logger.info(f"select files statement_id={stmt_id}")
    stmt_response = wait_for_statement(session_id, run_stmt_response["Id"])
    data_str = stmt_response["Statement"]["Output"]["Data"]["TextPlain"]
    files_metrics_columns = ["file_path","record_count", "file_size_in_bytes"]
    df = parse_statement_result(data_str,files_metrics_columns)
    
    file_metrics = {
        "avg_record_count": df["record_count"].astype(int).mean().astype(int),
        "max_record_count": df["record_count"].astype(int).max(),
        "min_record_count": df["record_count"].astype(int).min(),
        "deviation_record_count": df['record_count'].astype(int).std().round(2),
        "skew_record_count": df['record_count'].astype(int).skew().round(2),
        "avg_file_size": df['file_size_in_bytes'].astype(int).mean().astype(int),
        "max_file_size": df['file_size_in_bytes'].astype(int).max(),
        "min_file_size": df['file_size_in_bytes'].astype(int).min(),
    }
    logger.info("file_metrics=")
    logger.info(file_metrics)
    # loop over file_metrics, use key as metric name and value as metric value
    # loop over partition_metrics, use key as metric name and value as metric value
    for metric_name, metric_value in file_metrics.items():     
        logger.info(f"metric_name=files.{metric_name}, metric_value={metric_value.item()}")
        send_custom_metric(
            metric_name=f"files.{metric_name}",
            dimensions=[
                {'Name': 'table_name', 'Value': f"{glue_db_name}.{glue_table_name}"}
            ],
            value=metric_value.item(),
            unit='Bytes' if "size" in metric_name else "Count",
            namespace=os.getenv('CW_NAMESPACE'),
            timestamp = snapshot.timestamp_ms,
        )
    

def send_partition_metrics(snapshot,session_id):
    sql_stmt = f"select partition,record_count,file_count from glue_catalog.{glue_db_name}.{glue_table_name}.partitions"    
    run_stmt_response = glue_client.run_statement(
        SessionId=session_id,
        Code=f"df = spark.sql(\"{sql_stmt}\");df.show(df.count(),truncate=False)"
    )
    
    stmt_id = run_stmt_response["Id"]
    logger.info(f"send_partition_metrics() -> statement_id={stmt_id}")
    stmt_response = wait_for_statement(session_id, stmt_id)
    data_str = stmt_response["Statement"]["Output"]["Data"]["TextPlain"]
    partition_metrics_columns = ['partition', 'record_count', 'file_count']
    df = parse_statement_result(data_str,partition_metrics_columns)
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
    
    # loop over aggregated partition_metrics, use key as metric name and value as metric value
    for metric_name, metric_value in partition_metrics.items():  
        logger.info(f"metric_name=partitions.{metric_name}, metric_value={metric_value.item()}")
        send_custom_metric(
            metric_name=f"partitions.{metric_name}",
            dimensions=[
                {'Name': 'table_name', 'Value': f"{glue_db_name}.{glue_table_name}"}
            ],
            value=metric_value.item(),
            unit='Count',
            namespace=os.getenv('CW_NAMESPACE'),
            timestamp = snapshot.timestamp_ms,
        )
    
    for index, row in df.iterrows():
        partition_name = row['partition']
        record_count = row['record_count']
        file_count = row['file_count']
        logger.info(f"partition_name={partition_name}, record_count={record_count}, file_count={file_count}")
        
        send_custom_metric(
            metric_name=f"partitions.record_count",
            dimensions=[
                {'Name': 'table_name', 'Value': f"{glue_db_name}.{glue_table_name}"},
                {'Name': 'partition_name', 'Value': partition_name}
            ],
            value=int(record_count),
            unit='Count',
            namespace=os.getenv('CW_NAMESPACE'),
            timestamp = snapshot.timestamp_ms,
        )
        
        send_custom_metric(
            metric_name=f"partitions.file_count",
            dimensions=[
                {'Name': 'table_name', 'Value': f"{glue_db_name}.{glue_table_name}"},
                {'Name': 'partition_name', 'Value': partition_name}
            ],
            value=int(file_count),
            unit='Count',
            namespace=os.getenv('CW_NAMESPACE'),
            timestamp = snapshot.timestamp_ms,
        )

def create_or_reuse_glue_session():
    session_id = None
    
    glue_sessions = glue_client.list_sessions(
        Tags=glue_session_tags,
    )
    for session in glue_sessions["Sessions"]:
        if(session["Status"] == "READY"):
            session_id = session["Id"]
            logger.info(f"Found existing session_id={session_id}")
            break
    
    if(session_id is None):
        generated_uuid_string = str(uuid.uuid4())
        session_id = f"iceberg-metadata-lambda-{generated_uuid_string}"
        logger.info(f"No active Glue session found, creating new glue session with ID = {session_id}")
        glue_client.create_session(
            Id=session_id,
            Role=glue_service_role,
            Command={'Name': 'glueetl', "PythonVersion": "3"},
            Timeout=60,
            DefaultArguments={
                "--enable-glue-datacatalog": "true",
                "--enable-observability-metrics": "true",
                "--conf": f"spark.sql.catalog.glue_catalog=org.apache.iceberg.spark.SparkCatalog --conf spark.sql.catalog.glue_catalog.warehouse={warehouse_path} --conf spark.sql.catalog.glue_catalog.catalog-impl=org.apache.iceberg.aws.glue.GlueCatalog --conf spark.sql.catalog.glue_catalog.io-impl=org.apache.iceberg.aws.s3.S3FileIO --conf spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
                "--datalake-formats": "iceberg"
            },
            GlueVersion="4.0",
            NumberOfWorkers=2,
            WorkerType="G.1X",
            IdleTimeout=120,
            Tags=glue_session_tags,
        )
        wait_for_session(session_id)
    return session_id


def dt_to_ts(dt_str):
    dt_obj = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
    timestamp_seconds = dt_obj.timestamp()
    return int(timestamp_seconds * 1000)

def send_snapshot_metrics(snapshot_id, session_id):
    logger.info("send_snapshot_metrics")
    sql_stmt = f"select committed_at,snapshot_id,operation,summary from glue_catalog.{glue_db_name}.{glue_table_name}.snapshots where snapshot_id={snapshot_id}"
    run_stmt_response = glue_client.run_statement(
        SessionId=session_id,
        Code=f"df=spark.sql(\"{sql_stmt}\");json_rdd=df.toJSON();json_strings=json_rdd.collect();print(json_strings)"
    )
    stmt_id = run_stmt_response["Id"]
    logger.info(f"send_snapshot_metrics() -> statement_id={stmt_id}")
    stmt_response = wait_for_statement(session_id, stmt_id)
    json_list_str = stmt_response["Statement"]["Output"]["Data"]["TextPlain"].replace("\'", "")
    snapshots = json.loads(json_list_str)
    logger.info("send_snapshot_metrics()->response")
    logger.info(json.dumps(snapshots, indent=4))
    snapshot = snapshots[0]

    metrics = [
        "added-data-files", "added-records", "changed-partition-count", 
        "total-records","total-data-files", "total-delete-files",
        "added-files-size", "total-files-size", "added-position-deletes"
    ]
    for metric in metrics:
        normalized_metric_name = metric.replace("-", "_")
        if snapshot["summary"].get(metric) is None:
            snapshot["summary"][metric] = 0
        metric_value = snapshot["summary"][metric]
        timestamp_ms = dt_to_ts(snapshot["committed_at"])
        logger.info(f"metric_name=snapshot.{normalized_metric_name}, value={metric_value}")
        send_custom_metric(
            metric_name=f"snapshot.{normalized_metric_name}",
            dimensions=[
                {'Name': 'table_name', 'Value': f"{glue_db_name}.{glue_table_name}"}
            ],
            value=int(metric_value),
            unit='Bytes' if "size" in normalized_metric_name else "Count",
            namespace=os.getenv('CW_NAMESPACE'),
            timestamp = timestamp_ms,
        ) 


def lambda_handler(event, context):
    log_format = f"[{context.aws_request_id}:%(asctime)s.%(msecs)03d] %(message)s"
    logging.basicConfig(format=log_format, datefmt='%Y-%m-%d %H:%M:%S', level=logging.INFO)
    
    
    catalog = GlueCatalog("default")
    table = catalog.load_table((glue_db_name, glue_table_name))
    logger.info(f"current snapshot id={table.metadata.current_snapshot_id}")
    snapshot = table.metadata.snapshot_by_id(table.metadata.current_snapshot_id)
    
    logger.info("Using glue IS to produce metrics")
    session_id = create_or_reuse_glue_session()
    
    send_snapshot_metrics(table.metadata.current_snapshot_id, session_id)
    send_partition_metrics(snapshot,session_id)
    send_files_metrics(snapshot,session_id)