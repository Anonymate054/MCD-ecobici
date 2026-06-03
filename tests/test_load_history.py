"""
test_load_history.py — Unit tests for the backfill script (load_history.py).
No AWS calls needed here; all logic is pure Pandas transformation.
"""

import io
import sys
import os

import pandas as pd
import pytest

# Make src/backfill importable
REPO_ROOT    = os.path.dirname(os.path.dirname(__file__))
BACKFILL_DIR = os.path.join(REPO_ROOT, "src", "backfill")
if BACKFILL_DIR not in sys.path:
    sys.path.insert(0, BACKFILL_DIR)

import load_history as lh


# ── _month_range ─────────────────────────────────────────────────────────────

class TestMonthRange:
    def test_single_month(self):
        assert list(lh._month_range("2024-01", "2024-01")) == [(2024, 1)]

    def test_multi_month(self):
        result = list(lh._month_range("2024-01", "2024-03"))
        assert result == [(2024, 1), (2024, 2), (2024, 3)]

    def test_cross_year(self):
        result = list(lh._month_range("2023-11", "2024-02"))
        assert result == [(2023, 11), (2023, 12), (2024, 1), (2024, 2)]


# ── _sanitize ────────────────────────────────────────────────────────────────

class TestSanitize:
    def _make_df(self, extra_cols=None):
        data = {
            "Ciclo_Estacion_Retiro":  ["1", "2", "bad"],
            "Ciclo_Estacion_Arribo":  ["3", "4", "5"],
            "Fecha_Retiro":           ["01/01/2024"] * 3,
            "Hora_Retiro":            ["08:00:00"] * 3,
            "Fecha_Arribo":           ["01/01/2024"] * 3,
            "Hora_Arribo":            ["08:15:00"] * 3,
        }
        if extra_cols:
            data.update(extra_cols)
        return pd.DataFrame(data)

    def test_drops_pii_columns(self):
        df = self._make_df({"Genero_Usuario": ["M", "F", "M"],
                            "Edad_Usuario":   [25, 30, 22]})
        out = lh._sanitize(df, 2024, 1)
        assert "Genero_Usuario" not in out.columns
        assert "Edad_Usuario"   not in out.columns

    def test_coerces_and_drops_bad_station_ids(self):
        df  = self._make_df()   # "bad" in Ciclo_Estacion_Retiro → NaN → dropped
        out = lh._sanitize(df, 2024, 1)
        assert len(out) == 2    # row with "bad" is dropped

    def test_valid_rows_survive(self):
        df  = self._make_df()
        out = lh._sanitize(df, 2024, 1)
        assert set(out["Ciclo_Estacion_Retiro"].dropna()) == {1.0, 2.0}


# ── _compute_hourly_deltas ───────────────────────────────────────────────────

class TestComputeHourlyDeltas:
    def _make_trips(self):
        """10 trips: 5 from station 1 to station 2 at 08:xx, 5 from 2 to 1 at 09:xx."""
        rows = []
        for i in range(5):
            rows.append({
                "Ciclo_Estacion_Retiro": 1,
                "Ciclo_Estacion_Arribo": 2,
                "Fecha_Retiro": "01/01/2024",
                "Hora_Retiro":  f"08:0{i}:00",
                "Fecha_Arribo": "01/01/2024",
                "Hora_Arribo":  f"08:1{i}:00",
            })
        for i in range(5):
            rows.append({
                "Ciclo_Estacion_Retiro": 2,
                "Ciclo_Estacion_Arribo": 1,
                "Fecha_Retiro": "01/01/2024",
                "Hora_Retiro":  f"09:0{i}:00",
                "Fecha_Arribo": "01/01/2024",
                "Hora_Arribo":  f"09:1{i}:00",
            })
        return pd.DataFrame(rows)

    def test_output_schema(self):
        df  = self._make_trips()
        out = lh._compute_hourly_deltas(df)
        expected_cols = {
            "hour", "station_id", "avg_bikes_available", "avg_docks_available",
            "total_renting_minutes", "total_returning_minutes",
            "is_heuristically_broken", "temp_c", "precip_mm",
        }
        assert expected_cols.issubset(set(out.columns))

    def test_net_delta_computed_correctly(self):
        df  = self._make_trips()
        out = lh._compute_hourly_deltas(df)

        # At 08:00, station 1 had 5 departures → net_delta = 0 arrivals - 5 departures = -5
        s1_08 = out[(out["station_id"] == "1") & (out["hour"].dt.hour == 8)]
        assert not s1_08.empty
        assert s1_08.iloc[0]["avg_bikes_available"] == pytest.approx(-5.0)

        # At 09:00, station 1 had 5 arrivals → net_delta = +5
        s1_09 = out[(out["station_id"] == "1") & (out["hour"].dt.hour == 9)]
        assert not s1_09.empty
        assert s1_09.iloc[0]["avg_bikes_available"] == pytest.approx(5.0)

    def test_drops_unparseable_timestamps(self):
        df = pd.DataFrame([{
            "Ciclo_Estacion_Retiro": 1,
            "Ciclo_Estacion_Arribo": 2,
            "Fecha_Retiro": "INVALID",
            "Hora_Retiro":  "08:00:00",
            "Fecha_Arribo": "01/01/2024",
            "Hora_Arribo":  "08:15:00",
        }])
        out = lh._compute_hourly_deltas(df)
        # No valid trips → empty output
        assert len(out) == 0

    def test_heuristically_broken_defaults_false(self):
        out = lh._compute_hourly_deltas(self._make_trips())
        assert (out["is_heuristically_broken"] == False).all()

    def test_pii_not_in_output(self):
        out = lh._compute_hourly_deltas(self._make_trips())
        for col in lh.PII_COLUMNS:
            assert col not in out.columns
