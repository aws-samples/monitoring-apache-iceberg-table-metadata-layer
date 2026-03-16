import os
import time
import logging
import random
import pyarrow as pa
from pyiceberg.catalog import load_catalog
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, StringType, NestedField
from pyiceberg.partitioning import PartitionSpec, PartitionField
from pyiceberg.transforms import IdentityTransform
from trino.dbapi import connect

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def run_trino_query(cursor, query):
    logger.info(f"Executing Trino query: {query}")
    try:
        cursor.execute(query)
        return cursor.fetchall()
    except Exception as e:
        logger.error(f"Trino query failed: {e}")
        return None

def main():
    catalog_uri = os.getenv('CATALOG_URI', 'http://localhost:8181/')
    s3_endpoint = os.getenv('S3_ENDPOINT', 'http://localhost:9000')
    aws_access_key = os.getenv('AWS_ACCESS_KEY_ID', 'minio')
    aws_secret_key = os.getenv('AWS_SECRET_ACCESS_KEY', 'minio123')
    
    trino_host = os.getenv('TRINO_HOST', 'localhost')
    trino_port = int(os.getenv('TRINO_PORT', '8080'))
    trino_user = os.getenv('TRINO_USER', 'admin')
    
    logger.info("Connecting to Iceberg Rest Catalog...")
    catalog = load_catalog(
        "default",
        **{
            "type": "rest",
            "uri": catalog_uri,
            "s3.endpoint": s3_endpoint,
            "s3.access-key-id": aws_access_key,
            "s3.secret-access-key": aws_secret_key,
        }
    )
    
    try:
        catalog.create_namespace("demo")
        logger.info("Created namespace 'demo'")
    except Exception:
        logger.info("Namespace 'demo' already exists")
        
    # Use explicit Iceberg schema to avoid ID mapping issues
    iceberg_schema = Schema(
        NestedField(field_id=1, name="id", field_type=LongType(), required=False),
        NestedField(field_id=2, name="data", field_type=StringType(), required=False),
        NestedField(field_id=3, name="category", field_type=StringType(), required=False),
    )
    
    # Matching Arrow schema for data creation
    pa_schema = pa.schema([
        ('id', pa.int64()),
        ('data', pa.string()),
        ('category', pa.string())
    ])
    
    partition_spec = PartitionSpec(
        PartitionField(source_id=3, field_id=1000, transform=IdentityTransform(), name="category")
    )

    # try:
    #     catalog.drop_table("demo.events")
    #     logger.info("Dropped existing table 'demo.events' for clean state.")
    # except Exception:
    #     pass

    try:
        table = catalog.create_table_if_not_exists("demo.events", schema=iceberg_schema, partition_spec=partition_spec)
        logger.info("Created partitioned table 'demo.events'")
    except Exception as e:
        logger.error(f"Failed to create table: {e}")
        return
        
    # Connect to Trino for advanced operations
    logger.info(f"Connecting to Trino at {trino_host}:{trino_port}...")
    try:
        trino_conn = connect(
            host=trino_host,
            port=trino_port,
            user=trino_user,
            catalog='iceberg',
            schema='demo',
        )
        trino_cur = trino_conn.cursor()
    except Exception as e:
        logger.error(f"Failed to connect to Trino: {e}")
        return

    max_iterations = int(os.getenv('MAX_ITERATIONS', '20'))
    logger.info(f"Starting enhanced data generation loop for {max_iterations} iterations.")
    
    categories = ['A', 'B', 'C', 'D']
    
    try:
        for iteration in range(max_iterations):
            # 1. Random Appends
            batch_size = random.randint(5, 15)
            ids = [iteration * 100 + j for j in range(batch_size)]
            data = [f"val_{i}" for i in ids]
            cats = [random.choice(categories) for _ in range(batch_size)]
            
            df = pa.Table.from_arrays([ids, data, cats], schema=pa_schema)
            try:
                table.append(df)
                logger.info(f"Appended {batch_size} records to demo.events across random partitions.")
            except Exception as e:
                if "CommitFailedException" in str(e):
                    logger.warning(f"Commit conflict detected at iteration {iteration}. Skipping this batch.")
                else:
                    logger.error(f"Append failed: {e}")
            
            # 2. Occasional Deletes (every 5 iterations)
            if iteration > 0 and iteration % 5 == 0:
                cat_to_delete = random.choice(categories)
                query = f"DELETE FROM iceberg.demo.events WHERE category = '{cat_to_delete}' AND id % 2 = 0"
                run_trino_query(trino_cur, query)
                logger.info(f"Deleted records from category {cat_to_delete} via Trino.")

            # 3. Occasional Snapshot Expiration (every 10 iterations)
            if iteration > 0 and iteration % 10 == 0:
                # Expire snapshots older than 1 minute for the demo
                query = "ALTER TABLE iceberg.demo.events EXECUTE expire_snapshots(retention_threshold => '1m')"
                run_trino_query(trino_cur, query)
                logger.info("Executed snapshot expiration via Trino.")

            # 5. Occasional Orphan File Removal (every 12 iterations)
            if iteration > 0 and iteration % 12 == 0:
                query = "ALTER TABLE iceberg.demo.events EXECUTE remove_orphan_files(retention_threshold => '1m')"
                run_trino_query(trino_cur, query)
                logger.info("Executed orphan file removal via Trino.")

            # 6. Occasional Manifest Rewriting (every 15 iterations)
            if iteration > 0 and iteration % 15 == 0:
                query = "ALTER TABLE iceberg.demo.events EXECUTE rewrite_manifests"
                run_trino_query(trino_cur, query)
                logger.info("Executed manifest rewriting via Trino.")

            # 7. General Table Optimization (every 18 iterations)
            if iteration > 0 and iteration % 18 == 0:
                query = "ALTER TABLE iceberg.demo.events EXECUTE optimize"
                run_trino_query(trino_cur, query)
                logger.info("Executed general table optimization via Trino.")

            if iteration < max_iterations - 1:
                time.sleep(15)
                
        logger.info("Enhanced data generation completed successfully.")
    except KeyboardInterrupt:
        logger.info("Data generation stopped early by user.")
    finally:
        trino_cur.close()
        trino_conn.close()

if __name__ == "__main__":
    main()
