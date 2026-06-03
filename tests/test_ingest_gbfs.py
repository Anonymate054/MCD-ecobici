"""
test_ingest_gbfs.py — Unit tests for the ingest_gbfs Lambda.
AWS calls mocked via moto; HTTP calls mocked via unittest.mock (urllib.request).
"""

import importlib
import json
from datetime import date
from io import BytesIO
from unittest.mock import patch, MagicMock

import boto3
import pytest

from conftest import AWS_REGION, S3_BUCKET, SSM_REFRESH_PARAM

GBFS_BASE_URL = "https://gbfs.mex.lyft.com/gbfs/2.3/mex_mexico_city"

STATUS_PAYLOAD = {
    "data": {"stations": [
        {"station_id": "1", "num_bikes_available": 5, "num_docks_available": 10,
         "is_renting": 1, "is_returning": 1},
        {"station_id": "2", "num_bikes_available": 0, "num_docks_available": 15,
         "is_renting": 0, "is_returning": 0},
    ]}
}
INFO_PAYLOAD = {
    "data": {"stations": [
        {"station_id": "1", "name": "Reforma & Juárez",
         "lat": 19.4326, "lon": -99.1332, "capacity": 15},
    ]}
}


def _mod():
    import ingest_gbfs
    importlib.reload(ingest_gbfs)
    return ingest_gbfs


def _make_http_response(payload: dict, status: int = 200):
    """Return a mock context manager that mimics urllib urlopen."""
    body    = json.dumps(payload).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__  = MagicMock(return_value=False)
    return mock_resp


# ── Pure-function tests (no AWS) ────────────────────────────────────────────

class TestBuildStatusRecords:
    def test_normalises_fields(self):
        ts  = "2026-01-01T00:00:00Z"
        out = _mod()._build_status_records(
            [{"station_id": "42", "num_bikes_available": 3, "num_docks_available": 7,
              "is_renting": 1, "is_returning": 0}], ts
        )
        r = out[0]
        assert r["station_id"]      == "42"
        assert r["bikes_available"] == 3
        assert r["is_renting"]      is True
        assert r["is_returning"]    is False

    def test_missing_fields_default_to_zero(self):
        out = _mod()._build_status_records([{"station_id": "1"}], "2026-01-01T00:00:00Z")
        assert out[0]["bikes_available"] == 0
        assert out[0]["is_renting"]      is False

    def test_multiple_stations(self):
        raw = [{"station_id": str(i)} for i in range(10)]
        assert len(_mod()._build_status_records(raw, "t")) == 10


# ── SSM-gated refresh tests ─────────────────────────────────────────────────

class TestStationInfoRefresh:
    def test_returns_true_when_stale(self, secrets, ssm):
        assert _mod()._should_refresh_station_info() is True

    def test_returns_false_when_fresh(self, secrets, ssm):
        boto3.client("ssm", region_name=AWS_REGION).put_parameter(
            Name=SSM_REFRESH_PARAM, Value=date.today().isoformat(), Overwrite=True
        )
        assert _mod()._should_refresh_station_info() is False

    def test_mark_updates_ssm(self, secrets, ssm):
        _mod()._mark_station_info_refreshed()
        val = boto3.client("ssm", region_name=AWS_REGION).get_parameter(
            Name=SSM_REFRESH_PARAM)["Parameter"]["Value"]
        assert val == date.today().isoformat()


# ── S3 writer test ───────────────────────────────────────────────────────────

class TestWriteStationInfoToS3:
    def test_writes_valid_ndjson(self, s3_bucket, secrets, ssm):
        _mod()._write_station_info_to_s3(INFO_PAYLOAD["data"]["stations"], "2026-01-01T00:00:00Z")
        s3   = boto3.client("s3", region_name=AWS_REGION)
        objs = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix="raw/station_info/")
        assert objs["KeyCount"] >= 1
        body  = s3.get_object(Bucket=S3_BUCKET, Key=objs["Contents"][0]["Key"])["Body"].read()
        lines = [json.loads(l) for l in body.decode().strip().splitlines()]
        assert lines[0]["station_id"] == "1"
        assert lines[0]["lat"]        == pytest.approx(19.4326)


# ── Firehose batcher test ────────────────────────────────────────────────────

class TestPushToFirehose:
    def test_pushes_records(self, firehose_station, secrets, ssm):
        _mod()._push_to_firehose([{"station_id": str(i)} for i in range(5)])

    def test_batches_over_500(self, firehose_station, secrets, ssm):
        _mod()._push_to_firehose([{"station_id": str(i)} for i in range(1200)])


# ── Full handler integration ─────────────────────────────────────────────────

class TestHandler:
    def test_success_with_daily_refresh(self, s3_bucket, secrets, ssm, firehose_station):
        """Handler fetches status + triggers station_info refresh (SSM date is old)."""
        status_resp = _make_http_response(STATUS_PAYLOAD)
        info_resp   = _make_http_response(INFO_PAYLOAD)

        with patch("urllib.request.urlopen", side_effect=[status_resp, info_resp]):
            result = _mod().handler({}, None)

        assert result["statusCode"]      == 200
        assert result["stations_pushed"] == 2

        # SSM updated to today
        val = boto3.client("ssm", region_name=AWS_REGION).get_parameter(
            Name=SSM_REFRESH_PARAM)["Parameter"]["Value"]
        assert val == date.today().isoformat()

        # station_info written to S3
        s3   = boto3.client("s3", region_name=AWS_REGION)
        objs = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix="raw/station_info/")
        assert objs["KeyCount"] >= 1

    def test_skips_info_when_already_refreshed(self, s3_bucket, secrets, ssm, firehose_station):
        boto3.client("ssm", region_name=AWS_REGION).put_parameter(
            Name=SSM_REFRESH_PARAM, Value=date.today().isoformat(), Overwrite=True)

        status_resp = _make_http_response(STATUS_PAYLOAD)
        with patch("urllib.request.urlopen", return_value=status_resp):
            result = _mod().handler({}, None)

        assert result["statusCode"] == 200
        objs = boto3.client("s3", region_name=AWS_REGION).list_objects_v2(
            Bucket=S3_BUCKET, Prefix="raw/station_info/")
        assert objs.get("KeyCount", 0) == 0

    def test_raises_on_network_error(self, s3_bucket, secrets, ssm, firehose_station):
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("Connection refused")):
            with pytest.raises(urllib.error.URLError):
                _mod().handler({}, None)
