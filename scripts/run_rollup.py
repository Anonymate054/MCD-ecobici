#!/usr/bin/env python3
"""
run_rollup.py — Populate hourly_station_status Iceberg table
=============================================================
Steps:
  1. Create / refresh Athena views (vw_ecobici_weather_mapping, vw_broken_stations_summary).
  2. Run INSERT INTO hourly_station_status from raw_station_status + weather_observations.
  3. Print table stats.

Usage:
    export AWS_PROFILE=ecobici-de-01
    source .venv/bin/activate
    python scripts/run_rollup.py [--full-backfill] [--dry-run]
"""

import argparse
import time
from datetime import datetime, timezone

import boto3

# ── Config ────────────────────────────────────────────────────────────────────
REGION           = "us-east-1"
BUCKET           = "ecobici-datalake-195202617652"
GLUE_DATABASE    = "ecobici_lake"
ATHENA_WORKGROUP = "ecobici-workgroup"
RESULTS_PREFIX   = f"s3://{BUCKET}/athena-results/"

G = "\033[92m"; Y = "\033[93m"; C = "\033[96m"; B = "\033[1m"; X = "\033[0m"
OK = f"{G}✅{X}"; WARN = f"{Y}⚠️ {X}"; INFO = f"{C}ℹ️ {X}"; FAIL = f"\033[91m❌{X}"

athena = boto3.Session(region_name=REGION).client("athena")


def section(title: str) -> None:
    print(f"\n{B}{C}{'─'*62}{X}\n{B}{title}{X}\n{B}{C}{'─'*62}{X}")


def run_query(sql: str, label: str = "", dry_run: bool = False) -> list[dict]:
    if dry_run:
        print(f"\n  {Y}[DRY RUN — {label}]{X}\n{sql.strip()[:400]}...\n")
        return []

    resp    = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": GLUE_DATABASE},
        ResultConfiguration={"OutputLocation": RESULTS_PREFIX},
        WorkGroup=ATHENA_WORKGROUP,
    )
    exec_id = resp["QueryExecutionId"]
    print(f"  {INFO} [{label}] → {exec_id}")

    for _ in range(240):
        st     = athena.get_query_execution(QueryExecutionId=exec_id)
        status = st["QueryExecution"]["Status"]
        state  = status["State"]
        if state == "SUCCEEDED":
            stats   = st["QueryExecution"].get("Statistics", {})
            scanned = stats.get("DataScannedInBytes", 0)
            elapsed = stats.get("TotalExecutionTimeInMillis", 0)
            print(f"  {OK} SUCCEEDED  ({scanned/1024:.1f} KB, {elapsed/1000:.1f}s)")
            break
        if state in ("FAILED", "CANCELLED"):
            reason = status.get("StateChangeReason", "—")
            print(f"  {FAIL} {state}: {reason}")
            return []
        time.sleep(2)
    else:
        print(f"  {FAIL} Timeout waiting for query {exec_id}")
        return []

    rows = []
    paginator = athena.get_paginator("get_query_results")
    headers = None
    for page in paginator.paginate(QueryExecutionId=exec_id):
        result_rows = page["ResultSet"]["Rows"]
        if not result_rows:
            continue
        if headers is None:
            headers     = [c.get("VarCharValue", "") for c in result_rows[0]["Data"]]
            result_rows = result_rows[1:]
        for row in result_rows:
            rows.append({headers[i]: col.get("VarCharValue", "") for i, col in enumerate(row["Data"])})
    return rows


# ── SQL definitions ───────────────────────────────────────────────────────────

VIEW_WEATHER_MAPPING = """
CREATE OR REPLACE VIEW ecobici_lake.vw_ecobici_weather_mapping AS
SELECT
    e.station_id  AS ecobici_station_id,
    e.name        AS ecobici_name,
    e.lat         AS ecobici_lat,
    e.lon         AS ecobici_lon,
    e.station_id  AS weather_station_id,
    e.lat         AS weather_lat,
    e.lon         AS weather_lon,
    0.0           AS distance_m
FROM ecobici_lake.ecobici_station_info e
"""

VIEW_BROKEN_SUMMARY = """
CREATE OR REPLACE VIEW ecobici_lake.vw_broken_stations_summary AS
SELECT
    CAST(date_trunc('day', hour) AS DATE)  AS day,
    COUNT(DISTINCT station_id)             AS total_stations,
    COUNT(DISTINCT CASE WHEN is_heuristically_broken THEN station_id END) AS broken_stations,
    ROUND(
        100.0 * COUNT(DISTINCT CASE WHEN is_heuristically_broken THEN station_id END)
        / NULLIF(COUNT(DISTINCT station_id), 0), 2
    )                                      AS broken_pct,
    ROUND(SUM(avg_bikes_available), 0)     AS total_bikes_available
FROM ecobici_lake.hourly_station_status
GROUP BY 1
ORDER BY 1 DESC
"""


def build_rollup_sql(full_backfill: bool) -> str:
    """
    Correct Athena v3 syntax: INSERT INTO <table> WITH cte AS (...) SELECT ...
    NOT: WITH cte AS (...) INSERT INTO <table> SELECT ...
    """
    where_clause = (
        ""
        if full_backfill
        else 'WHERE "timestamp" >= DATE_ADD(\'hour\', -48, NOW())'
    )
    return f"""
INSERT INTO ecobici_lake.hourly_station_status
WITH
raw AS (
    SELECT
        date_trunc('hour', "timestamp")       AS hour,
        station_id,
        bikes_available,
        docks_available,
        is_renting,
        is_returning,
        (EXTRACT(HOUR FROM "timestamp" AT TIME ZONE 'America/Mexico_City')
            BETWEEN 7 AND 20)                 AS is_peak_hour
    FROM ecobici_lake.raw_station_status
    {where_clause}
),
hourly_agg AS (
    SELECT
        hour,
        station_id,
        ROUND(AVG(CAST(bikes_available AS DOUBLE)), 2)  AS avg_bikes_available,
        ROUND(AVG(CAST(docks_available AS DOUBLE)), 2)  AS avg_docks_available,
        SUM(CASE WHEN is_renting   THEN 5 ELSE 0 END)  AS total_renting_minutes,
        SUM(CASE WHEN is_returning THEN 5 ELSE 0 END)  AS total_returning_minutes,
        BOOL_AND(NOT is_renting AND NOT is_returning)  AS native_malfunction,
        STDDEV(CAST(bikes_available AS DOUBLE))         AS bikes_stddev,
        AVG(CAST(bikes_available  AS DOUBLE))           AS avg_bikes,
        MAX(CASE WHEN is_peak_hour THEN 1 ELSE 0 END)  AS during_peak
    FROM raw
    GROUP BY 1, 2
),
frozen_window AS (
    SELECT
        hour, station_id,
        avg_bikes, bikes_stddev, during_peak, native_malfunction,
        avg_bikes_available, avg_docks_available,
        total_renting_minutes, total_returning_minutes,
        LAG(bikes_stddev, 1) OVER (PARTITION BY station_id ORDER BY hour) AS stddev_lag1,
        LAG(bikes_stddev, 2) OVER (PARTITION BY station_id ORDER BY hour) AS stddev_lag2,
        LAG(avg_bikes,    1) OVER (PARTITION BY station_id ORDER BY hour) AS avg_bikes_lag1,
        LAG(avg_bikes,    2) OVER (PARTITION BY station_id ORDER BY hour) AS avg_bikes_lag2
    FROM hourly_agg
),
flagged AS (
    SELECT
        fw.hour, fw.station_id,
        fw.avg_bikes_available, fw.avg_docks_available,
        fw.total_renting_minutes, fw.total_returning_minutes,
        (
            fw.native_malfunction
            OR (
                fw.during_peak = 1
                AND fw.avg_bikes    > 0
                AND fw.bikes_stddev < 0.5
                AND fw.stddev_lag1  IS NOT NULL AND fw.stddev_lag1 < 0.5
                AND fw.stddev_lag2  IS NOT NULL AND fw.stddev_lag2 < 0.5
                AND fw.avg_bikes_lag1 IS NOT NULL
                AND fw.avg_bikes_lag2 IS NOT NULL
            )
        ) AS is_heuristically_broken
    FROM frozen_window fw
),
weather_hourly AS (
    SELECT
        date_trunc('hour', "timestamp") AS hour,
        station_id,
        AVG(temp_c)    AS temp_c,
        SUM(precip_mm) AS precip_mm
    FROM ecobici_lake.weather_observations
    WHERE _is_filled = FALSE
    GROUP BY 1, 2
)
SELECT
    f.hour,
    f.station_id,
    f.avg_bikes_available,
    f.avg_docks_available,
    f.total_renting_minutes,
    f.total_returning_minutes,
    f.is_heuristically_broken,
    w.temp_c,
    COALESCE(w.precip_mm, 0.0) AS precip_mm
FROM flagged f
LEFT JOIN weather_hourly w
    ON  w.hour       = f.hour
    AND w.station_id = f.station_id
ORDER BY f.hour, f.station_id
"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full-backfill", action="store_true",
                        help="Process ALL raw data (not just last 48h)")
    parser.add_argument("--dry-run",       action="store_true",
                        help="Print SQL without executing")
    args = parser.parse_args()
    dry  = args.dry_run

    print(f"\n{B}{G}╔══════════════════════════════════════════════════════╗")
    print(f"║  Ecobici — Rollup: hourly_station_status             ║")
    print(f"╚══════════════════════════════════════════════════════╝{X}")
    print(f"  Timestamp : {datetime.now(timezone.utc).isoformat()}")
    print(f"  Mode      : {'FULL BACKFILL' if args.full_backfill else 'last 48h'}")
    print(f"  Dry run   : {dry}")

    # ── Step 1: Views ──────────────────────────────────────────────────────────
    section("Step 1 — Create / refresh Athena views")
    run_query(VIEW_WEATHER_MAPPING, "vw_ecobici_weather_mapping", dry)
    run_query(VIEW_BROKEN_SUMMARY,  "vw_broken_stations_summary",  dry)

    # ── Step 2: Pre-count ──────────────────────────────────────────────────────
    pre_n = 0
    if not dry:
        section("Step 2 — Pre-rollup row count")
        pre = run_query(
            "SELECT COUNT(*) AS cnt FROM ecobici_lake.hourly_station_status",
            "pre-count"
        )
        pre_n = int(pre[0]["cnt"]) if pre else 0
        print(f"  hourly_station_status has {B}{pre_n}{X} rows before rollup")

    # ── Step 3: Rollup INSERT ──────────────────────────────────────────────────
    section("Step 3 — INSERT INTO hourly_station_status")
    sql = build_rollup_sql(args.full_backfill)
    run_query(sql, "rollup INSERT", dry)

    if dry:
        print(f"\n  {WARN} Dry run — no data written.")
        return

    # ── Step 4: Post-count + stats ─────────────────────────────────────────────
    section("Step 4 — Verify results")
    post = run_query(
        "SELECT COUNT(*) AS cnt FROM ecobici_lake.hourly_station_status",
        "post-count"
    )
    post_n = int(post[0]["cnt"]) if post else 0
    new_n  = post_n - pre_n
    print(f"  {OK} hourly_station_status now has {B}{post_n}{X} rows  (+{new_n} new)\n")

    if post_n == 0:
        print(f"  {WARN} Still 0 rows — check raw data availability above")
        return

    stats = run_query("""
SELECT
    MIN(hour)                                         AS earliest_hour,
    MAX(hour)                                         AS latest_hour,
    COUNT(DISTINCT station_id)                        AS unique_stations,
    COUNT(DISTINCT CAST(hour AS DATE))                AS days_covered,
    SUM(CAST(is_heuristically_broken AS INTEGER))     AS broken_station_hours,
    ROUND(AVG(avg_bikes_available), 2)                AS network_avg_bikes,
    ROUND(AVG(temp_c), 2)                             AS avg_temp_c,
    ROUND(SUM(precip_mm), 2)                          AS total_precip_mm
FROM ecobici_lake.hourly_station_status
""", "aggregate stats")

    if stats:
        r = stats[0]
        print(f"  {B}Rollup stats:{X}")
        print(f"    Earliest hour        : {r.get('earliest_hour','—')}")
        print(f"    Latest hour          : {r.get('latest_hour','—')}")
        print(f"    Unique stations      : {r.get('unique_stations','—')}")
        print(f"    Days covered         : {r.get('days_covered','—')}")
        print(f"    Broken station-hours : {r.get('broken_station_hours','—')}")
        print(f"    Network avg bikes    : {r.get('network_avg_bikes','—')}")
        print(f"    Avg temperature      : {r.get('avg_temp_c','—')} °C")
        print(f"    Total precipitation  : {r.get('total_precip_mm','—')} mm")

    sample = run_query("""
SELECT hour, station_id, avg_bikes_available, avg_docks_available,
       total_renting_minutes, is_heuristically_broken, temp_c, precip_mm
FROM ecobici_lake.hourly_station_status
ORDER BY hour DESC, station_id
LIMIT 8
""", "sample rows")

    if sample:
        print(f"\n  {B}Sample rows (most recent hour):{X}")
        for row in sample:
            broken = "🔴" if row.get("is_heuristically_broken") == "true" else "🟢"
            bikes  = float(row["avg_bikes_available"]) if row["avg_bikes_available"] else 0.0
            docks  = float(row["avg_docks_available"]) if row["avg_docks_available"] else 0.0
            temp   = row.get("temp_c", "—")
            precip = row.get("precip_mm", "—")
            print(f"    {broken} {row['hour'][:16]}  stn={row['station_id']:>4} "
                  f" bikes={bikes:.1f}  docks={docks:.1f}"
                  f"  temp={temp}°C  precip={precip}mm")

    print(f"\n  {B}{G}Rollup complete ✅{X}\n")


if __name__ == "__main__":
    main()
