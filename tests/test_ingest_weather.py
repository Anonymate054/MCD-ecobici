"""
test_ingest_weather.py — Unit tests for the ingest_weather Lambda.
Focuses on the micro-cleaning logic (temp bounds + forward-fill).
HTTP calls mocked via unittest.mock (urllib.request).
"""

import importlib
import json
from unittest.mock import patch, MagicMock

import pytest

from conftest import FIREHOSE_WEATHER_STREAM

WEATHER_URL = "https://smn.conagua.gob.mx/tools/GUI/webservices/"


def _mod():
    import ingest_weather
    importlib.reload(ingest_weather)
    return ingest_weather


def _make_http_response(payload: dict, status: int = 200):
    body      = json.dumps(payload).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.status      = status
    mock_resp.read.return_value = body
    mock_resp.__enter__   = lambda s: s
    mock_resp.__exit__    = MagicMock(return_value=False)
    return mock_resp


# ── _validate_temp ───────────────────────────────────────────────────────────

class TestValidateTemp:
    def test_valid_temp_passes(self):
        m = _mod()
        assert m._validate_temp(20.0)  == pytest.approx(20.0)
        assert m._validate_temp(-10.0) == pytest.approx(-10.0)
        assert m._validate_temp(50.0)  == pytest.approx(50.0)

    def test_out_of_bounds_returns_none(self):
        m = _mod()
        assert m._validate_temp(-11.0) is None
        assert m._validate_temp(50.1)  is None
        assert m._validate_temp(999.0) is None

    def test_none_returns_none(self):
        assert _mod()._validate_temp(None) is None

    def test_non_numeric_string_returns_none(self):
        assert _mod()._validate_temp("bad") is None

    def test_numeric_string_coerced(self):
        assert _mod()._validate_temp("25.5") == pytest.approx(25.5)


# ── _micro_clean ─────────────────────────────────────────────────────────────

class TestMicroClean:
    def _obs(self, station_id, temp_c, precip_mm=0.0, ts="2026-01-01T00:00:00Z"):
        return {"station_id": station_id, "temp_c": temp_c,
                "precip_mm": precip_mm, "timestamp": ts}

    def test_valid_obs_passes_through(self):
        out = _mod()._micro_clean([self._obs("S1", 22.5)])
        assert len(out) == 1
        assert out[0]["temp_c"]     == pytest.approx(22.5)
        assert out[0]["_is_filled"] is False

    def test_invalid_temp_dropped_when_no_prior(self):
        out = _mod()._micro_clean([self._obs("S1", 999.0)])
        assert len(out) == 0

    def test_forward_fill_applied_up_to_1_slot(self):
        ts = [f"2026-01-01T00:{i*10:02d}:00Z" for i in range(4)]
        raw = [self._obs("S1", 25.0, ts=ts[0])] + \
              [self._obs("S1", None, ts=ts[i]) for i in range(1, 4)]
        out = _mod()._micro_clean(raw)
        assert len(out) == 2  # 1 valid + 1 filled; 2 dropped
        assert out[1]["_is_filled"] is True
        assert out[1]["temp_c"] == pytest.approx(25.0)

    def test_forward_fill_stops_after_1_slot(self):
        ts  = [f"2026-01-01T00:{i*10:02d}:00Z" for i in range(6)]
        raw = [self._obs("S1", 25.0, ts=ts[0])] + \
              [self._obs("S1", None, ts=ts[i]) for i in range(1, 6)]
        out = _mod()._micro_clean(raw)
        assert len(out) == 2   # 1 valid + 1 filled; 4 dropped


    def test_fill_resets_after_valid_reading(self):
        ts = [f"2026-01-01T00:{i*10:02d}:00Z" for i in range(4)]
        raw = [
            self._obs("S1", 25.0, ts=ts[0]),
            self._obs("S1", None, ts=ts[1]),
            self._obs("S1", 28.0, ts=ts[2]),
            self._obs("S1", None, ts=ts[3]),
        ]
        out = _mod()._micro_clean(raw)
        assert len(out) == 4
        assert out[3]["temp_c"] == pytest.approx(28.0)

    def test_multiple_stations_isolated(self):
        raw = [
            self._obs("S1", 20.0, ts="2026-01-01T00:00:00Z"),
            self._obs("S2", None, ts="2026-01-01T00:00:00Z"),
        ]
        out = _mod()._micro_clean(raw)
        assert len(out) == 1
        assert out[0]["station_id"] == "S1"


# ── Full handler integration ─────────────────────────────────────────────────

class TestWeatherHandler:
    def test_handler_success(self, monkeypatch, secrets, firehose_weather):
        # Override FIREHOSE_STREAM_NAME to the weather-specific stream
        monkeypatch.setenv("FIREHOSE_STREAM_NAME", FIREHOSE_WEATHER_STREAM)
        from datetime import datetime, timezone
        now_hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00")
        payload = {
            "hourly": {
                "time": [now_hour],
                "temperature_2m": [22.0],
                "precipitation": [0.0]
            }
        }
        with patch("urllib.request.urlopen", return_value=_make_http_response(payload)):
            result = _mod().handler({}, None)

        assert result["statusCode"]      == 200
        assert result["raw_received"]    == 1
        assert result["records_pushed"]  == 1
        assert result["records_dropped"] == 0

    def test_handler_drops_invalid_temps(self, secrets, firehose_weather):
        from datetime import datetime, timezone
        now_hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00")
        payload = {
            "hourly": {
                "time": [now_hour],
                "temperature_2m": [999.0],
                "precipitation": [0.0]
            }
        }
        with patch("urllib.request.urlopen", return_value=_make_http_response(payload)):
            result = _mod().handler({}, None)

        assert result["records_pushed"]  == 0
        assert result["records_dropped"] == 1

    def test_handler_empty_observations(self, secrets, firehose_weather):
        payload = {
            "hourly": {
                "time": [],
                "temperature_2m": [],
                "precipitation": []
            }
        }
        with patch("urllib.request.urlopen",
                   return_value=_make_http_response(payload)):
            result = _mod().handler({}, None)

        assert result["statusCode"]     == 200
        assert result["records_pushed"] == 0

    def test_handler_raises_on_network_error(self, secrets, firehose_weather):
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("Timeout")):
            with pytest.raises(urllib.error.URLError):
                _mod().handler({}, None)

