"""
loader.py — Iceberg Table Loader Lambda
========================================
Triggered every 30 minutes by EventBridge.

Performs three incremental operations:
  1. Load raw station_status Firehose JSON → INSERT INTO raw_station_status Iceberg
     (only rows with timestamp > current MAX in the Iceberg table — no duplicates)
  2. Load raw weather Firehose JSON → INSERT INTO weather_observations Iceberg
     (only rows with timestamp > current MAX in the Iceberg table — no duplicates)
  3. Run incremental rollup: INSERT INTO hourly_station_status
     (processes last 3 hours of raw data to cover any partial-hour windows)

Architecture:
  Firehose (60s buffer) → S3 raw JSON files
  This Lambda (every 30 min) → Athena staging → INSERT INTO Iceberg tables

Env vars (injected by Terraform):
  GLUE_DATABASE    - Glue catalog database name
  ATHENA_WORKGROUP - Athena workgroup name
  S3_BUCKET        - Data lake S3 bucket name
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------
athena_client = boto3.client("athena")
s3_client     = boto3.client("s3")
cw_client     = boto3.client("cloudwatch")

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
GLUE_DATABASE    = os.environ["GLUE_DATABASE"]
ATHENA_WORKGROUP = os.environ["ATHENA_WORKGROUP"]
S3_BUCKET        = os.environ["S3_BUCKET"]

RESULTS_PREFIX   = f"s3://{S3_BUCKET}/athena-results/"
POLL_INTERVAL    = 3
MAX_WAIT_SECS    = 270   # Stay within 300s Lambda timeout


# ---------------------------------------------------------------------------
# Athena helpers
# ---------------------------------------------------------------------------

def _run_query(sql: str, description: str) -> list[dict]:
    """Submit an Athena query, wait for it, return rows as list[dict]."""
    logger.info("Athena [%s]: submitting...", description)
    resp    = athena_client.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": GLUE_DATABASE},
        ResultConfiguration={"OutputLocation": RESULTS_PREFIX},
        WorkGroup=ATHENA_WORKGROUP,
    )
    exec_id = resp["QueryExecutionId"]

    elapsed = 0
    while elapsed < MAX_WAIT_SECS:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        st     = athena_client.get_query_execution(QueryExecutionId=exec_id)
        status = st["QueryExecution"]["Status"]
        state  = status["State"]

        if state == "SUCCEEDED":
            stats    = st["QueryExecution"].get("Statistics", {})
            scanned  = stats.get("DataScannedInBytes", 0)
            duration = stats.get("TotalExecutionTimeInMillis", 0)
            logger.info(
                "Athena [%s]: SUCCEEDED (%.1f KB, %.1fs)",
                description, scanned / 1024, duration / 1000,
            )
            break

        if state in ("FAILED", "CANCELLED"):
            reason = status.get("StateChangeReason", "—")
            raise RuntimeError(f"Athena [{description}] {state}: {reason}")

    else:
        raise TimeoutError(f"Athena [{description}] timed out after {MAX_WAIT_SECS}s")

    # Fetch results (only SELECT queries return rows; DDL/DML return empty)
    rows    = []
    pager   = athena_client.get_paginator("get_query_results")
    headers = None
    for page in pager.paginate(QueryExecutionId=exec_id):
        result_rows = page["ResultSet"]["Rows"]
        if not result_rows:
            continue
        if headers is None:
            headers     = [c.get("VarCharValue", "") for c in result_rows[0]["Data"]]
            result_rows = result_rows[1:]
        for row in result_rows:
            rows.append(
                {headers[i]: col.get("VarCharValue", "") for i, col in enumerate(row["Data"])}
            )
    return rows


def _scalar(rows: list[dict], key: str, default: str = "0") -> str:
    """Extract a scalar value from the first row of a query result."""
    return rows[0].get(key, default) if rows else default


def _publish_metric(operation: str, rows_inserted: int, duration_ms: float, success: bool) -> None:
    """Publish a custom CloudWatch metric for loader tracking."""
    try:
        cw_client.put_metric_data(
            Namespace="EcobiciDataLake",
            MetricData=[
                {
                    "MetricName": "LoaderRowsInserted",
                    "Dimensions": [
                        {"Name": "Operation", "Value": operation},
                        {"Name": "Status",    "Value": "success" if success else "failure"},
                    ],
                    "Value":     float(rows_inserted),
                    "Unit":      "Count",
                    "Timestamp": datetime.now(timezone.utc),
                },
                {
                    "MetricName": "LoaderDurationMs",
                    "Dimensions": [
                        {"Name": "Operation", "Value": operation},
                    ],
                    "Value":     duration_ms,
                    "Unit":      "Milliseconds",
                    "Timestamp": datetime.now(timezone.utc),
                },
            ],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to publish CloudWatch metric: %s", exc)


# ---------------------------------------------------------------------------
# Step 1 — Load raw station_status → raw_station_status Iceberg
# ---------------------------------------------------------------------------

def _load_station_status(now: datetime) -> dict:
    """
    Incrementally load today's raw Firehose JSON files into the Iceberg table.
    Only inserts rows with timestamp > the current MAX in the Iceberg table.
    """
    t0     = time.monotonic()
    prefix = (
        f"s3://{S3_BUCKET}/raw/station_status/"
        f"year={now.year}/month={now.month:02d}/day={now.day:02d}/"
    )
    staging = "staging_loader_station_status"

    try:
        # Check current latest timestamp in Iceberg
        latest_rows = _run_query(
            'SELECT MAX("timestamp") AS max_ts FROM raw_station_status',
            "station_status:max_ts"
        )
        latest_ts = _scalar(latest_rows, "max_ts", "1970-01-01 00:00:00.000000")
        logger.info("station_status: Iceberg latest = %s", latest_ts)

        # Create staging external table over today's S3 prefix
        _run_query(f'DROP TABLE IF EXISTS {staging}',
                   "station_status:drop_staging")
        _run_query(f"""
CREATE EXTERNAL TABLE {staging} (
    timestamp        STRING,
    station_id       STRING,
    bikes_available  INT,
    docks_available  INT,
    is_renting       BOOLEAN,
    is_returning     BOOLEAN,
    ingest_at        STRING
)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
WITH SERDEPROPERTIES ('mapping.ingest_at' = '_ingest_at')
LOCATION '{prefix}'
TBLPROPERTIES ('ignore.malformed.json' = 'true')
""",                 "station_status:create_staging")

        # Count new rows in staging
        count_rows = _run_query(
            f"""
SELECT COUNT(*) AS cnt
FROM {staging}
WHERE station_id IS NOT NULL
  AND station_id != ''
  AND CAST(from_iso8601_timestamp(timestamp) AS TIMESTAMP)
      > TIMESTAMP '{latest_ts}'
""",            "station_status:count_new"
        )
        new_count = int(_scalar(count_rows, "cnt", "0"))
        logger.info("station_status: %d new rows to insert", new_count)

        if new_count == 0:
            logger.info("station_status: already up-to-date, skipping INSERT")
            _run_query(f'DROP TABLE IF EXISTS {staging}',
                       "station_status:drop_staging")
            return {"operation": "load_station_status", "rows_inserted": 0, "status": "skipped"}

        # INSERT only new rows
        _run_query(f"""
INSERT INTO raw_station_status
SELECT
    CAST(from_iso8601_timestamp(timestamp)  AS TIMESTAMP) AS timestamp,
    station_id,
    bikes_available,
    docks_available,
    is_renting,
    is_returning,
    CAST(from_iso8601_timestamp(ingest_at)  AS TIMESTAMP) AS _ingest_at
FROM {staging}
WHERE station_id IS NOT NULL
  AND station_id != ''
  AND CAST(from_iso8601_timestamp(timestamp) AS TIMESTAMP)
      > TIMESTAMP '{latest_ts}'
""",                 "station_status:insert")

        _run_query(f'DROP TABLE IF EXISTS {staging}',
                   "station_status:drop_staging")

        duration_ms = (time.monotonic() - t0) * 1000
        _publish_metric("load_station_status", new_count, duration_ms, success=True)
        logger.info("station_status: inserted %d rows (%.1fs)", new_count, duration_ms / 1000)
        return {"operation": "load_station_status", "rows_inserted": new_count, "status": "success"}

    except Exception as exc:  # noqa: BLE001
        duration_ms = (time.monotonic() - t0) * 1000
        _publish_metric("load_station_status", 0, duration_ms, success=False)
        logger.error("load_station_status failed: %s", exc, exc_info=True)
        try:
            _run_query(f'DROP TABLE IF EXISTS {staging}', "station_status:cleanup")
        except Exception:  # noqa: BLE001
            pass
        return {"operation": "load_station_status", "rows_inserted": 0, "status": "failed", "error": str(exc)}


# ---------------------------------------------------------------------------
# Step 2 — Load raw weather → weather_observations Iceberg
# ---------------------------------------------------------------------------

def _load_weather(now: datetime) -> dict:
    """
    Incrementally load today's raw weather Firehose JSON files into the Iceberg table.
    """
    t0      = time.monotonic()
    prefix  = (
        f"s3://{S3_BUCKET}/raw/weather/"
        f"year={now.year}/month={now.month:02d}/day={now.day:02d}/"
    )
    staging = "staging_loader_weather"

    # Check if there are any raw weather files today
    resp  = s3_client.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix.replace(f"s3://{S3_BUCKET}/", ""))
    files = [obj for obj in resp.get("Contents", []) if obj["Size"] > 0]
    if not files:
        logger.info("weather: no raw files in S3 today, skipping")
        return {"operation": "load_weather", "rows_inserted": 0, "status": "skipped"}

    try:
        latest_rows = _run_query(
            'SELECT MAX("timestamp") AS max_ts FROM weather_observations',
            "weather:max_ts"
        )
        latest_ts = _scalar(latest_rows, "max_ts", "1970-01-01 00:00:00.000000")
        logger.info("weather: Iceberg latest = %s", latest_ts)

        _run_query(f'DROP TABLE IF EXISTS {staging}', "weather:drop_staging")
        _run_query(f"""
CREATE EXTERNAL TABLE {staging} (
    timestamp   STRING,
    station_id  STRING,
    temp_c      DOUBLE,
    precip_mm   DOUBLE,
    is_filled   STRING
)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
WITH SERDEPROPERTIES ('mapping.is_filled' = '_is_filled')
LOCATION '{prefix}'
TBLPROPERTIES ('ignore.malformed.json' = 'true')
""",             "weather:create_staging")

        count_rows = _run_query(
            f"""
SELECT COUNT(*) AS cnt
FROM {staging}
WHERE station_id IS NOT NULL AND station_id != ''
  AND CAST(from_iso8601_timestamp(timestamp) AS TIMESTAMP)
      > TIMESTAMP '{latest_ts}'
""",         "weather:count_new"
        )
        new_count = int(_scalar(count_rows, "cnt", "0"))
        logger.info("weather: %d new rows to insert", new_count)

        if new_count == 0:
            logger.info("weather: already up-to-date, skipping INSERT")
            _run_query(f'DROP TABLE IF EXISTS {staging}', "weather:drop_staging")
            return {"operation": "load_weather", "rows_inserted": 0, "status": "skipped"}

        _run_query(f"""
INSERT INTO weather_observations
SELECT
    CAST(from_iso8601_timestamp(timestamp) AS TIMESTAMP) AS timestamp,
    station_id,
    temp_c,
    precip_mm,
    CAST(is_filled AS BOOLEAN)                           AS _is_filled
FROM {staging}
WHERE station_id IS NOT NULL AND station_id != ''
  AND CAST(from_iso8601_timestamp(timestamp) AS TIMESTAMP)
      > TIMESTAMP '{latest_ts}'
""",             "weather:insert")

        _run_query(f'DROP TABLE IF EXISTS {staging}', "weather:drop_staging")

        duration_ms = (time.monotonic() - t0) * 1000
        _publish_metric("load_weather", new_count, duration_ms, success=True)
        logger.info("weather: inserted %d rows (%.1fs)", new_count, duration_ms / 1000)
        return {"operation": "load_weather", "rows_inserted": new_count, "status": "success"}

    except Exception as exc:  # noqa: BLE001
        duration_ms = (time.monotonic() - t0) * 1000
        _publish_metric("load_weather", 0, duration_ms, success=False)
        logger.error("load_weather failed: %s", exc, exc_info=True)
        try:
            _run_query(f'DROP TABLE IF EXISTS {staging}', "weather:cleanup")
        except Exception:  # noqa: BLE001
            pass
        return {"operation": "load_weather", "rows_inserted": 0, "status": "failed", "error": str(exc)}


# ---------------------------------------------------------------------------
# Step 3 — Incremental rollup → hourly_station_status Iceberg
# ---------------------------------------------------------------------------

def _run_rollup() -> dict:
    """
    Incremental rollup: aggregate the last 3 hours of raw station_status +
    weather_observations into hourly_station_status.
    Uses a 3-hour window to handle partial hours and Firehose lag.
    """
    t0 = time.monotonic()
    try:
        _run_query("""
INSERT INTO hourly_station_status
WITH
raw AS (
    SELECT
        date_trunc('hour', "timestamp")      AS hour,
        station_id,
        bikes_available,
        docks_available,
        is_renting,
        is_returning,
        (EXTRACT(HOUR FROM "timestamp" AT TIME ZONE 'America/Mexico_City')
            BETWEEN 7 AND 20)               AS is_peak_hour
    FROM raw_station_status
    WHERE "timestamp" >= DATE_ADD('hour', -3, NOW())
),
hourly_agg AS (
    SELECT
        hour, station_id,
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
        hour, station_id,
        avg_bikes_available, avg_docks_available,
        total_renting_minutes, total_returning_minutes,
        (
            native_malfunction
            OR (
                during_peak = 1
                AND avg_bikes    > 0
                AND bikes_stddev < 0.5
                AND stddev_lag1  IS NOT NULL AND stddev_lag1 < 0.5
                AND stddev_lag2  IS NOT NULL AND stddev_lag2 < 0.5
                AND avg_bikes_lag1 IS NOT NULL
                AND avg_bikes_lag2 IS NOT NULL
            )
        ) AS is_heuristically_broken
    FROM frozen_window
),
weather_hourly AS (
    SELECT
        date_trunc('hour', "timestamp") AS hour,
        station_id,
        AVG(temp_c)    AS temp_c,
        SUM(precip_mm) AS precip_mm
    FROM weather_observations
    WHERE _is_filled = FALSE
      AND "timestamp" >= DATE_ADD('hour', -3, NOW())
    GROUP BY 1, 2
),
new_hours AS (
    SELECT DISTINCT f.hour, f.station_id
    FROM flagged f
    WHERE NOT EXISTS (
        SELECT 1
        FROM hourly_station_status h
        WHERE h.hour       = f.hour
          AND h.station_id = f.station_id
    )
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
JOIN new_hours n
    ON n.hour = f.hour AND n.station_id = f.station_id
LEFT JOIN weather_hourly w
    ON  w.hour       = f.hour
    AND w.station_id = f.station_id
ORDER BY f.hour, f.station_id
""",                "rollup:insert")

        duration_ms = (time.monotonic() - t0) * 1000
        _publish_metric("rollup", 0, duration_ms, success=True)
        logger.info("rollup: SUCCEEDED (%.1fs)", duration_ms / 1000)
        return {"operation": "rollup", "status": "success", "duration_ms": round(duration_ms)}

    except Exception as exc:  # noqa: BLE001
        duration_ms = (time.monotonic() - t0) * 1000
        _publish_metric("rollup", 0, duration_ms, success=False)
        logger.error("rollup failed: %s", exc, exc_info=True)
        return {"operation": "rollup", "status": "failed", "error": str(exc)}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handler(event: Any, context: Any) -> dict:
    """Lambda entry point."""
    now       = datetime.now(timezone.utc)
    run_id    = now.strftime("%Y%m%dT%H%M%SZ")
    failures  = 0
    results   = []

    logger.info("Loader run started: %s", run_id)

    # Step 1: station_status
    r = _load_station_status(now)
    results.append(r)
    if r["status"] == "failed":
        failures += 1

    # Step 2: weather observations
    r = _load_weather(now)
    results.append(r)
    if r["status"] == "failed":
        failures += 1

    # Step 3: hourly rollup (only if step 1 loaded new data or status was skipped with recent data)
    r = _run_rollup()
    results.append(r)
    if r["status"] == "failed":
        failures += 1

    summary = {
        "statusCode":  200 if failures == 0 else 207,
        "run_id":      run_id,
        "failures":    failures,
        "results":     results,
    }
    logger.info("Loader run complete: %s", json.dumps(summary))
    return summary
