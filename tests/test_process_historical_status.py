"""
test_process_historical_status.py — Unit tests for process_historical_status Lambda.
==================================================================================
Verifies simulation logic, Athena queries, Glue table operations, and S3 staging.
"""

import csv
import importlib
import io
import json
from unittest.mock import MagicMock, patch

import pytest


def _mod():
    import process_historical_status
    importlib.reload(process_historical_status)
    return process_historical_status


# Mock classes for testing context
class MockContext:
    def __init__(self):
        self.aws_request_id = "test-req-id-123"


# ── Test Suite ───────────────────────────────────────────────────────────────

class TestProcessHistoricalStatus:
    @pytest.fixture
    def mock_env(self, monkeypatch):
        monkeypatch.setenv("GLUE_DATABASE", "ecobici_lake")
        monkeypatch.setenv("ATHENA_WORKGROUP", "main")
        monkeypatch.setenv("S3_BUCKET", "ecobici-datalake-test")

    def _make_athena_status(self, state):
        return {
            "QueryExecution": {
                "Status": {"State": state, "StateChangeReason": "test"},
                "Statistics": {"DataScannedInBytes": 1024},
            }
        }

    def test_handler_success(self, mock_env):
        m = _mod()
        
        # 1. Mock Athena Client
        mock_athena = MagicMock()
        mock_athena.start_query_execution.return_value = {"QueryExecutionId": "qid-123"}
        mock_athena.get_query_execution.return_value = self._make_athena_status("SUCCEEDED")
        
        # 2. Mock S3 Client
        mock_s3 = MagicMock()
        
        # Capacities CSV result mock
        cap_csv = "station_id,capacity\n1,10\n2,20\n"
        # Flow CSV result mock
        flow_csv = (
            "timestamp,station_id,checkouts,checkins\n"
            "2026-05-01 00:00:00,1,2,0\n"
            "2026-05-01 00:15:00,1,0,3\n"
            "2026-05-01 00:00:00,2,5,0\n"
        )
        
        def mock_get_object(Bucket, Key):
            assert Bucket == "ecobici-datalake-test"
            if "qid-123.csv" in Key:
                # We need to return either capacities or flows depending on which query was run.
                # In our code, capacities is run first, then flows. We can differentiate by tracking calls
                # or checking the query execution if we mock more, but a simple index-based state is easy:
                mock_get_object.call_count += 1
                body = cap_csv if mock_get_object.call_count == 1 else flow_csv
                return {"Body": io.BytesIO(body.encode("utf-8"))}
            raise FileNotFoundError(f"Key {Key} not found")
        
        mock_get_object.call_count = 0
        mock_s3.get_object.side_effect = mock_get_object
        
        # Capture the uploaded staging CSV
        uploaded_csv_content = {}
        def mock_put_object(Bucket, Key, Body, ContentType):
            assert Bucket == "ecobici-datalake-test"
            assert Key.startswith("tmp/staging_historical_15m/")
            uploaded_csv_content["data"] = Body.decode("utf-8")
            return {}
        mock_s3.put_object.side_effect = mock_put_object

        # 3. Mock Glue Client
        mock_glue = MagicMock()

        # 4. Patch and run handler
        with patch.object(m, "athena_client", mock_athena), \
             patch.object(m, "s3_client", mock_s3), \
             patch.object(m, "glue_client", mock_glue), \
             patch("time.sleep"):
             
            event = {"year": "2026", "month": "05"}
            res = m.handler(event, MockContext())
            
        assert res["statusCode"] == 200
        
        # Verify Glue create/delete tables were called
        mock_glue.create_table.assert_called_once()
        mock_glue.delete_table.assert_called_once()
        mock_s3.delete_object.assert_called_once()
        
        # Parse and verify the simulated CSV data
        assert "data" in uploaded_csv_content
        reader = csv.DictReader(uploaded_csv_content["data"].splitlines())
        rows = list(reader)
        
        # We expect a complete grid for 2 stations for the month of May 2026
        # May has 31 days * 96 slots/day = 2976 slots. 2 stations * 2976 slots = 5952 rows.
        assert len(rows) == 5952
        
        # Verify bounded flow values for station 1 (capacity 10)
        # Initial: 50% capacity = 5 bikes
        # Row 1 (00:00:00): checkouts=2, checkins=0 => net_delta=-2 => bikes = 5 - 2 = 3. Docks = 7.
        row_0 = next(r for r in rows if r["station_id"] == "1" and r["timestamp"] == "2026-05-01 00:00:00")
        assert int(row_0["checkouts"]) == 2
        assert int(row_0["checkins"]) == 0
        assert int(row_0["net_delta"]) == -2
        assert float(row_0["estimated_bikes_available"]) == 3.0
        assert float(row_0["estimated_docks_available"]) == 7.0
        assert row_0["station_state"] == "NORMAL"
        
        # Row 2 (00:15:00): checkouts=0, checkins=3 => net_delta=3 => bikes = 3 + 3 = 6. Docks = 4.
        row_1 = next(r for r in rows if r["station_id"] == "1" and r["timestamp"] == "2026-05-01 00:15:00")
        assert int(row_1["checkouts"]) == 0
        assert int(row_1["checkins"]) == 3
        assert int(row_1["net_delta"]) == 3
        assert float(row_1["estimated_bikes_available"]) == 6.0
        assert float(row_1["estimated_docks_available"]) == 4.0
        assert row_1["station_state"] == "NORMAL"

        # Verify bounded flow values for station 2 (capacity 20)
        # Initial: 50% capacity = 10 bikes
        # Row 1 (00:00:00): checkouts=5, checkins=0 => net_delta=-5 => bikes = 10 - 5 = 5. Docks = 15.
        row_s2 = next(r for r in rows if r["station_id"] == "2" and r["timestamp"] == "2026-05-01 00:00:00")
        assert int(row_s2["checkouts"]) == 5
        assert int(row_s2["checkins"]) == 0
        assert int(row_s2["net_delta"]) == -5
        assert float(row_s2["estimated_bikes_available"]) == 5.0
        assert float(row_s2["estimated_docks_available"]) == 15.0
        assert row_s2["station_state"] == "NORMAL"

    def test_handler_cleanup_on_error(self, mock_env):
        m = _mod()
        
        mock_athena = MagicMock()
        mock_athena.start_query_execution.return_value = {"QueryExecutionId": "qid-123"}
        mock_athena.get_query_execution.return_value = self._make_athena_status("SUCCEEDED")
        
        mock_s3 = MagicMock()
        cap_csv = "station_id,capacity\n1,10\n"
        flow_csv = "timestamp,station_id,checkouts,checkins\n"
        
        def mock_get_object(Bucket, Key):
            body = cap_csv if mock_get_object.call_count == 0 else flow_csv
            mock_get_object.call_count += 1
            return {"Body": io.BytesIO(body.encode("utf-8"))}
            
        mock_get_object.call_count = 0
        mock_s3.get_object.side_effect = mock_get_object
        
        # Raise an exception on Athena DML call during INSERT stage
        def mock_start_query_execution(QueryString, **kwargs):
            if "INSERT INTO" in QueryString:
                return {"QueryExecutionId": "qid-failed"}
            return {"QueryExecutionId": "qid-123"}
            
        def mock_get_query_execution(QueryExecutionId):
            if QueryExecutionId == "qid-failed":
                return self._make_athena_status("FAILED")
            return self._make_athena_status("SUCCEEDED")
            
        mock_athena.start_query_execution.side_effect = mock_start_query_execution
        mock_athena.get_query_execution.side_effect = mock_get_query_execution

        mock_glue = MagicMock()

        # Patch and run handler, expecting it to raise RuntimeError
        with patch.object(m, "athena_client", mock_athena), \
             patch.object(m, "s3_client", mock_s3), \
             patch.object(m, "glue_client", mock_glue), \
             patch("time.sleep"):
             
            with pytest.raises(RuntimeError, match="Insert staging to target 15m"):
                m.handler({"ym": "2026-05"}, MockContext())
                
        # Even on failure, cleanup must be executed (staging table deleted, S3 file deleted)
        mock_glue.delete_table.assert_called_once()
        mock_s3.delete_object.assert_called_once()
