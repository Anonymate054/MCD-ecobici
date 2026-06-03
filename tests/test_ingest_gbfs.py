"""
test_ingest_gbfs.py — Unit tests for the refactored ingest_gbfs Lambda.
The GBFS feed is public — no Secrets Manager dependency.
HTTP calls mocked via unittest.mock (urllib.request).
AWS calls mocked via moto (S3, SSM, Firehose).
"""

import importlib
import json
from datetime import date
from unittest.mock import patch, MagicMock

import boto3
import pytest

from conftest import AWS_REGION, S3_BUCKET, SSM_REFRESH_PARAM, GBFS_DISCOVERY_URL

# ── Payloads mirroring the real Lyft/Ecobici GBFS schema ────────────────────

DISCOVERY_PAYLOAD = {
    "last_updated": 1780464933,
    "ttl": 10,
    "data": {
        "en": {
            "feeds": [
                {"name": "station_status",
                 "url": "https://gbfs.mex.lyftbikes.com/gbfs/en/station_status.json"},
                {"name": "station_information",
                 "url": "https://gbfs.mex.lyftbikes.com/gbfs/en/station_information.json"},
            ]
        }
    }
}

STATUS_PAYLOAD = {
    "data": {"stations": [
        {"station_id": "1",  "num_bikes_available": 5,  "num_docks_available": 10,
         "is_renting": 1, "is_returning": 1},
        {"station_id": "2",  "num_bikes_available": 0,  "num_docks_available": 15,
         "is_renting": 0, "is_returning": 0},
    ]}
}

INFO_PAYLOAD = {
    "data": {"stations": [
        {"station_id": "1", "name": "CE-710 Molino del Rey",
         "lat": 19.416795, "lon": -99.192508, "capacity": 39},
    ]}
}


def _mod():
    import ingest_gbfs
    importlib.reload(ingest_gbfs)
    return ingest_gbfs


def _mock_resp(payload: dict, status: int = 200):
    body      = json.dumps(payload).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.status            = status
    mock_resp.read.return_value = body
    mock_resp.__enter__         = lambda s: s
    mock_resp.__exit__          = MagicMock(return_value=False)
    return mock_resp


# ── _discover_feed_urls ──────────────────────────────────────────────────────

class TestDiscoverFeedUrls:
    def test_returns_en_feed_map(self):
        with patch("urllib.request.urlopen", return_value=_mock_resp(DISCOVERY_PAYLOAD)):
            url_map = _mod()._discover_feed_urls()
        assert "station_status"      in url_map
        assert "station_information" in url_map
        assert "lyftbikes.com" in url_map["station_status"]

    def test_falls_back_to_first_language(self):
        """If 'en' is absent, uses the first available language."""
        payload_es_only = {
            "data": {"es": {"feeds": [
                {"name": "station_status", "url": "https://example.com/es/station_status.json"}
            ]}}
        }
        with patch("urllib.request.urlopen", return_value=_mock_resp(payload_es_only)):
            url_map = _mod()._discover_feed_urls()
        assert "station_status" in url_map


# ── _build_status_records ────────────────────────────────────────────────────

class TestBuildStatusRecords:
    def test_normalises_fields(self):
        ts  = "2026-01-01T00:00:00Z"
        out = _mod()._build_status_records(
            [{"station_id": "42", "num_bikes_available": 3,
              "num_docks_available": 7, "is_renting": 1, "is_returning": 0}], ts
        )
        r = out[0]
        assert r["station_id"]      == "42"
        assert r["bikes_available"] == 3
        assert r["is_renting"]      is True
        assert r["is_returning"]    is False
        assert r["timestamp"]       == ts

    def test_missing_fields_default_to_zero(self):
        out = _mod()._build_status_records([{"station_id": "1"}], "t")
        assert out[0]["bikes_available"] == 0
        assert out[0]["is_renting"]      is False

    def test_multiple_stations(self):
        raw = [{"station_id": str(i)} for i in range(10)]
        assert len(_mod()._build_status_records(raw, "t")) == 10


# ── SSM refresh gate ─────────────────────────────────────────────────────────

class TestStationInfoRefresh:
    def test_returns_true_when_stale(self, ssm):
        assert _mod()._should_refresh_station_info() is True

    def test_returns_false_when_fresh(self, ssm):
        boto3.client("ssm", region_name=AWS_REGION).put_parameter(
            Name=SSM_REFRESH_PARAM, Value=date.today().isoformat(), Overwrite=True
        )
        assert _mod()._should_refresh_station_info() is False

    def test_mark_updates_ssm(self, ssm):
        _mod()._mark_station_info_refreshed()
        val = boto3.client("ssm", region_name=AWS_REGION).get_parameter(
            Name=SSM_REFRESH_PARAM)["Parameter"]["Value"]
        assert val == date.today().isoformat()


# ── S3 writer ─────────────────────────────────────────────────────────────────

class TestWriteStationInfoToS3:
    def test_writes_valid_ndjson(self, s3_bucket, ssm):
        _mod()._write_station_info_to_s3(
            INFO_PAYLOAD["data"]["stations"], "2026-01-01T00:00:00Z"
        )
        s3   = boto3.client("s3", region_name=AWS_REGION)
        objs = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix="raw/station_info/")
        assert objs["KeyCount"] >= 1
        body  = s3.get_object(Bucket=S3_BUCKET, Key=objs["Contents"][0]["Key"])["Body"].read()
        lines = [json.loads(l) for l in body.decode().strip().splitlines()]
        assert lines[0]["station_id"] == "1"
        assert lines[0]["lat"]        == pytest.approx(19.416795)


# ── Firehose batcher ──────────────────────────────────────────────────────────

class TestPushToFirehose:
    def test_pushes_records(self, firehose_station, ssm):
        _mod()._push_to_firehose([{"station_id": str(i)} for i in range(5)])

    def test_batches_over_500(self, firehose_station, ssm):
        _mod()._push_to_firehose([{"station_id": str(i)} for i in range(1200)])


# ── Full handler integration ──────────────────────────────────────────────────

class TestHandler:
    """
    Handler integration tests. HTTP mocked via side_effect list:
      call 1 → discovery (gbfs.json)
      call 2 → station_status.json
      call 3 → station_information.json  (only when SSM date is stale)
    """

    def test_success_with_daily_refresh(self, s3_bucket, ssm, firehose_station):
        side_effects = [
            _mock_resp(DISCOVERY_PAYLOAD),
            _mock_resp(STATUS_PAYLOAD),
            _mock_resp(INFO_PAYLOAD),
        ]
        with patch("urllib.request.urlopen", side_effect=side_effects):
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

    def test_skips_info_when_already_refreshed(self, s3_bucket, ssm, firehose_station):
        boto3.client("ssm", region_name=AWS_REGION).put_parameter(
            Name=SSM_REFRESH_PARAM, Value=date.today().isoformat(), Overwrite=True
        )
        # Only 2 HTTP calls: discovery + station_status (no info fetch)
        with patch("urllib.request.urlopen", side_effect=[
            _mock_resp(DISCOVERY_PAYLOAD),
            _mock_resp(STATUS_PAYLOAD),
        ]):
            result = _mod().handler({}, None)

        assert result["statusCode"] == 200
        objs = boto3.client("s3", region_name=AWS_REGION).list_objects_v2(
            Bucket=S3_BUCKET, Prefix="raw/station_info/")
        assert objs.get("KeyCount", 0) == 0

    def test_raises_on_network_error(self, s3_bucket, ssm, firehose_station):
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("Connection refused")):
            with pytest.raises(urllib.error.URLError):
                _mod().handler({}, None)

    def test_raises_when_status_feed_missing(self, s3_bucket, ssm, firehose_station):
        """If station_status is absent from the discovery doc, handler raises."""
        no_status = {
            "data": {"en": {"feeds": [
                {"name": "station_information",
                 "url": "https://gbfs.mex.lyftbikes.com/gbfs/en/station_information.json"}
            ]}}
        }
        with patch("urllib.request.urlopen", return_value=_mock_resp(no_status)):
            with pytest.raises(RuntimeError, match="station_status feed not found"):
                _mod().handler({}, None)
