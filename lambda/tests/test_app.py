import os
import shutil
import unittest
from numbers import Number
from unittest.mock import patch
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, StringType, NestedField
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.catalog.sql import SqlCatalog
import numpy as np
import pyarrow as pa
from app import send_files_metrics, send_partition_metrics, send_snapshot_metrics, normalize_metrics

# Mock AWS credentials
os.environ['AWS_ACCESS_KEY_ID'] = 'testing'
os.environ['AWS_SECRET_ACCESS_KEY'] = 'testing'
os.environ['AWS_SECURITY_TOKEN'] = 'testing'
os.environ['AWS_SESSION_TOKEN'] = 'testing'

class TestIcebergMetrics(unittest.TestCase):

    @patch.dict(os.environ, {'CW_NAMESPACE': 'TestNamespace'})
    def setUp(self):
        self.schema = Schema(
            NestedField(1, 'id', LongType(), False),
            NestedField(2, 'data', StringType(), False)
        )
        self.partition_spec = PartitionSpec(
            fields=[
                PartitionField(source_id=2, field_id=1000, name="data", transform="identity")
            ]
        )
    
        catalog_path = './tests/test_db'
        if os.path.exists(catalog_path):
            shutil.rmtree(catalog_path)
        os.makedirs(catalog_path)
        warehouse_path = os.path.abspath(catalog_path)
        self.catalog = SqlCatalog(
            "default",
            **{
                "uri": f"sqlite:///{warehouse_path}/pyiceberg_catalog.db",
                "warehouse": f"file://{warehouse_path}",
            },
        )
        self.catalog.create_namespace('default')
        self.catalog.create_table(
            'default.test_table',
            schema=self.schema,
            partition_spec=self.partition_spec
        )
        
        # Load the table and insert some data
        self.table = self.catalog.load_table(('default', 'test_table'))
        self.update_table(0, 5)

    
    def create_arrow_table(self, range_start, range_end):
        data = {
            'id': pa.array(range(range_start, range_end), pa.int64()),
            'data': pa.array(['data' + str(i) for i in range(range_start, range_end)], pa.string())
        }
        return pa.Table.from_pydict(data)
    
    
    def assert_metrics(self, expected, table, snapshot, method_to_test):
        def send_metrics_stub(metrics, namespace, table, snapshot):
            normalized_metrics = normalize_metrics(metrics)
            self.assertDictEqual(normalized_metrics, expected)
        
        with patch('app.send_metrics', side_effect=send_metrics_stub):
            method_to_test(table, snapshot)
    
    
    def test_send_files_metrics(self):
        expected_file_metrics = {'avg_record_count': 1, 'max_record_count': 1, 'min_record_count': 1, 'avg_file_size': 1068, 'max_file_size': 1068, 'min_file_size': 1068}
        self.assert_metrics(expected_file_metrics, self.table, self.snapshot, send_files_metrics)
    
    
    @patch('app.send_custom_metric')
    def test_send_partition_metrics(self, mock_send_custom_metric):
        expected_partition_metrics = {'avg_record_count': 1, 'max_record_count': 1, 'min_record_count': 1, 'deviation_record_count': 0.0, 'skew_record_count': 0.0, 'avg_file_count': 1, 'max_file_count': 1, 'min_file_count': 1, 'deviation_file_count': 0.0, 'skew_file_count': 0.0}
        self.assert_metrics(expected_partition_metrics, self.table, self.snapshot, send_partition_metrics)
    
    
    def test_send_snapshot_metrics(self):
        expected_snapshot_metrics = {'added_data_files': 5, 'added_records': 5, 'changed_partition_count': 5, 'total_records': 5, 'total_data_files': 5, 'total_delete_files': 0, 'added_files_size': 5340, 'total_files_size': 5340, 'added_position_deletes': 0}
        self.assert_metrics(expected_snapshot_metrics, self.table, self.snapshot, send_snapshot_metrics)
    
    
    def update_table(self, range_start, range_end):
        # Perform an update operation on the Iceberg table
        arrow_table = self.create_arrow_table(range_start, range_end)
        self.table.append(arrow_table)
        self.table.refresh()
        self.snapshot = self.table.current_snapshot()
    
    
    @patch('app.send_custom_metric')
    def test_metrics_after_update(self, mock_send_custom_metric):
        self.update_table(5, 10)
        
        expected_file_metrics = {'avg_record_count': 1, 'max_record_count': 1, 'min_record_count': 1, 'avg_file_size': 1068, 'max_file_size': 1068, 'min_file_size': 1068}
        self.assert_metrics(expected_file_metrics, self.table, self.snapshot, send_files_metrics)
        
        expected_partition_metrics = {'avg_record_count': 1, 'max_record_count': 1, 'min_record_count': 1, 'deviation_record_count': 0.0, 'skew_record_count': 0.0, 'avg_file_count': 1, 'max_file_count': 1, 'min_file_count': 1, 'deviation_file_count': 0.0, 'skew_file_count': 0.0}
        self.assert_metrics(expected_partition_metrics, self.table, self.snapshot, send_partition_metrics)
        
        expected_snapshot_metrics = {'added_data_files': 5, 'added_records': 5, 'changed_partition_count': 5, 'total_records': 10, 'total_data_files': 10, 'total_delete_files': 0, 'added_files_size': 5340, 'total_files_size': 10680, 'added_position_deletes': 0}
        self.assert_metrics(expected_snapshot_metrics, self.table, self.snapshot, send_snapshot_metrics)


if __name__ == '__main__':
    unittest.main()
