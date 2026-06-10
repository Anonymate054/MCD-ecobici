"""
test_maintenance.py — Unit tests for the maintenance Lambda.
Athena is mocked: we intercept start_query_execution and get_query_execution
so the polling loop resolves immediately without real Athena.
"""

import importlib
import json
from unittest.mock import MagicMock, patch, call

import pytest

from conftest import GLUE_DATABASE, ATHENA_WORKGROUP, SNAPSHOT_DAYS


def _mod():
    import maintenance
    importlib.reload(maintenance)
    return maintenance


# ── _run_athena_query ────────────────────────────────────────────────────────

class TestRunAthenaQuery:
    def test_submits_correct_sql(self, aws_mock):
        m = _mod()
        mock_athena = MagicMock()
        mock_athena.start_query_execution.return_value = {"QueryExecutionId": "test-id-123"}

        with patch.object(m, "athena_client", mock_athena):
            exec_id = m._run_athena_query("SELECT 1", "test-op")

        assert exec_id == "test-id-123"
        mock_athena.start_query_execution.assert_called_once()
        call_kwargs = mock_athena.start_query_execution.call_args[1]
        assert call_kwargs["QueryString"]                         == "SELECT 1"
        assert call_kwargs["QueryExecutionContext"]["Database"]    == GLUE_DATABASE
        assert call_kwargs["WorkGroup"]                           == ATHENA_WORKGROUP


# ── _wait_for_query ──────────────────────────────────────────────────────────

class TestWaitForQuery:
    def _make_status(self, state, scanned=0):
        return {
            "QueryExecution": {
                "Status": {"State": state, "StateChangeReason": "test"},
                "Statistics": {"DataScannedInBytes": scanned},
            }
        }

    def test_succeeds_on_first_poll(self, aws_mock):
        m = _mod()
        mock_athena = MagicMock()
        mock_athena.get_query_execution.return_value = self._make_status("SUCCEEDED", 1024)

        with patch.object(m, "athena_client", mock_athena), \
             patch("time.sleep"):
            result = m._wait_for_query("exec-id", "test")

        assert result["QueryExecution"]["Status"]["State"] == "SUCCEEDED"

    def test_raises_on_failure(self, aws_mock):
        m = _mod()
        mock_athena = MagicMock()
        mock_athena.get_query_execution.return_value = self._make_status("FAILED")

        with patch.object(m, "athena_client", mock_athena), \
             patch("time.sleep"):
            with pytest.raises(RuntimeError, match="FAILED"):
                m._wait_for_query("exec-id", "test")

    def test_raises_on_cancellation(self, aws_mock):
        m = _mod()
        mock_athena = MagicMock()
        mock_athena.get_query_execution.return_value = self._make_status("CANCELLED")

        with patch.object(m, "athena_client", mock_athena), \
             patch("time.sleep"):
            with pytest.raises(RuntimeError, match="CANCELLED"):
                m._wait_for_query("exec-id", "test")

    def test_retries_while_running(self, aws_mock):
        m = _mod()
        mock_athena = MagicMock()
        mock_athena.get_query_execution.side_effect = [
            self._make_status("RUNNING"),
            self._make_status("RUNNING"),
            self._make_status("SUCCEEDED"),
        ]

        with patch.object(m, "athena_client", mock_athena), \
             patch("time.sleep"):
            result = m._wait_for_query("exec-id", "test")

        assert mock_athena.get_query_execution.call_count == 3


# ── _run_optimize & _run_expire_snapshots ────────────────────────────────────

class TestMaintenanceOperations:
    def _mock_athena_success(self, mod):
        """Return a mock athena client that immediately reports SUCCEEDED."""
        mock_athena = MagicMock()
        mock_athena.start_query_execution.return_value = {"QueryExecutionId": "qid"}
        mock_athena.get_query_execution.return_value = {
            "QueryExecution": {
                "Status": {"State": "SUCCEEDED"},
                "Statistics": {"DataScannedInBytes": 512},
            }
        }
        return mock_athena

    def test_optimize_returns_success(self, aws_mock):
        m           = _mod()
        mock_athena = self._mock_athena_success(m)
        mock_cw     = MagicMock()

        with patch.object(m, "athena_client", mock_athena), \
             patch.object(m, "cw_client", mock_cw), \
             patch("time.sleep"):
            result = m._run_optimize("raw_station_status")

        assert result["status"]    == "success"
        assert result["operation"] == "OPTIMIZE"
        assert result["table"]     == "raw_station_status"

        # Verify SQL contains BIN_PACK
        sql_used = mock_athena.start_query_execution.call_args[1]["QueryString"]
        assert "BIN_PACK"           in sql_used
        assert "raw_station_status" in sql_used

    def test_expire_snapshots_returns_success(self, aws_mock):
        m           = _mod()
        mock_athena = self._mock_athena_success(m)
        mock_cw     = MagicMock()

        with patch.object(m, "athena_client", mock_athena), \
             patch.object(m, "cw_client", mock_cw), \
             patch("time.sleep"):
            result = m._run_expire_snapshots("weather_observations")

        assert result["status"]    == "success"
        assert result["operation"] == "EXPIRE_SNAPSHOTS"
        sql_used = mock_athena.start_query_execution.call_args[1]["QueryString"]
        assert "expire_snapshots"       in sql_used
        assert "weather_observations"   in sql_used
        assert f"'{SNAPSHOT_DAYS}d'"    in sql_used

    def test_optimize_isolates_failure(self, aws_mock):
        """A failed OPTIMIZE returns status=failed but does NOT raise."""
        m           = _mod()
        mock_athena = MagicMock()
        mock_athena.start_query_execution.return_value = {"QueryExecutionId": "qid"}
        mock_athena.get_query_execution.return_value = {
            "QueryExecution": {
                "Status": {"State": "FAILED", "StateChangeReason": "Simulated failure"},
                "Statistics": {},
            }
        }
        mock_cw = MagicMock()

        with patch.object(m, "athena_client", mock_athena), \
             patch.object(m, "cw_client", mock_cw), \
             patch("time.sleep"):
            result = m._run_optimize("hourly_station_status")

        assert result["status"] == "failed"
        assert "error" in result


# ── Full handler ─────────────────────────────────────────────────────────────

class TestMaintenanceHandler:
    def _make_success_athena(self, mod):
        mock_athena = MagicMock()
        mock_athena.start_query_execution.return_value = {"QueryExecutionId": "qid"}
        mock_athena.get_query_execution.return_value = {
            "QueryExecution": {
                "Status": {"State": "SUCCEEDED"},
                "Statistics": {"DataScannedInBytes": 1024},
            }
        }
        return mock_athena

    def test_handler_processes_all_tables(self, aws_mock):
        m           = _mod()
        mock_athena = self._make_success_athena(m)
        mock_cw     = MagicMock()

        with patch.object(m, "athena_client", mock_athena), \
             patch.object(m, "cw_client", mock_cw), \
             patch("time.sleep"):
            result = m.handler({}, None)

        assert result["statusCode"]          == 200
        assert result["tables_processed"]    == 5   # 5 managed tables
        assert result["operations_total"]    == 10  # 2 ops × 5 tables
        assert result["failures"]            == 0

    def test_handler_returns_207_on_partial_failure(self, aws_mock):
        m = _mod()
        call_count = {"n": 0}

        def start_query(*args, **kwargs):
            call_count["n"] += 1
            return {"QueryExecutionId": f"qid-{call_count['n']}"}

        def get_query(*args, **kwargs):
            # Fail the first operation, succeed all others
            n = int(kwargs.get("QueryExecutionId", "qid-1").split("-")[-1])
            state = "FAILED" if n == 1 else "SUCCEEDED"
            return {
                "QueryExecution": {
                    "Status": {"State": state, "StateChangeReason": "test"},
                    "Statistics": {"DataScannedInBytes": 0},
                }
            }

        mock_athena = MagicMock()
        mock_athena.start_query_execution.side_effect = start_query
        mock_athena.get_query_execution.side_effect   = get_query
        mock_cw = MagicMock()

        with patch.object(m, "athena_client", mock_athena), \
             patch.object(m, "cw_client", mock_cw), \
             patch("time.sleep"):
            result = m.handler({}, None)

        assert result["statusCode"] == 207
        assert result["failures"]   == 1
