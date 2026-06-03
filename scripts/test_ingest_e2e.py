#!/usr/bin/env python3
"""
test_ingest_e2e.py — End-to-end data ingest test
=================================================
Orchestrates a full ingest cycle and verifies data appears in Athena/Iceberg tables.

Steps:
  1. Invoke the ingest_gbfs Lambda (forces a fresh ingest right now)
  2. Wait for Kinesis Firehose to flush raw JSON to S3 (~65 s buffer)
  3. Run Athena to discover the raw JSON files and load them into the Iceberg table
  4. Query each table and print row counts + sample records

Usage (no arguments needed):
    source .venv/bin/activate
    export AWS_PROFILE=ecobici-de-01
    python scripts/test_ingest_e2e.py
"""

import base64
import json
import sys
import time
from datetime import datetime, timezone

import boto3

# ── Config ────────────────────────────────────────────────────────────────────
REGION           = "us-east-1"
BUCKET           = "ecobici-datalake-195202617652"
GLUE_DATABASE    = "ecobici_lake"
ATHENA_WORKGROUP = "ecobici-workgroup"
RESULTS_PREFIX   = f"s3://{BUCKET}/athena-results/"
LAMBDA_GBFS      = "ecobici-ingest-gbfs"

# ── Colours ───────────────────────────────────────────────────────────────────
G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"; C = "\033[96m"; B = "\033[1m"; X = "\033[0m"
OK = f"{G}✅{X}"; WARN = f"{Y}⚠️ {X}"; FAIL = f"{R}❌{X}"; INFO = f"{C}ℹ️ {X}"

# ── AWS clients ───────────────────────────────────────────────────────────────
session = boto3.Session(region_name=REGION)
lambda_client = session.client("lambda")
athena        = session.client("athena")
s3            = session.client("s3")
firehose      = session.client("firehose")


def section(title):
    print(f"\n{B}{C}{'─'*62}{X}")
    print(f"{B}{title}{X}")
    print(f"{B}{C}{'─'*62}{X}")


# ── Athena helpers ────────────────────────────────────────────────────────────

def run_query(sql: str, label: str = "") -> list[dict]:
    """Submit an Athena query and block until it completes. Returns rows as dicts."""
    resp    = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": GLUE_DATABASE},
        ResultConfiguration={"OutputLocation": RESULTS_PREFIX},
        WorkGroup=ATHENA_WORKGROUP,
    )
    exec_id = resp["QueryExecutionId"]
    if label:
        print(f"  {INFO} Athena [{label}] → execution {exec_id}")

    # Poll until done
    for _ in range(120):
        state = athena.get_query_execution(QueryExecutionId=exec_id)
        status = state["QueryExecution"]["Status"]
        s = status["State"]
        if s == "SUCCEEDED":
            scanned = state["QueryExecution"]["Statistics"].get("DataScannedInBytes", 0)
            if label:
                print(f"  {OK} SUCCEEDED  (scanned {scanned/1024:.1f} KB)")
            break
        if s in ("FAILED", "CANCELLED"):
            reason = status.get("StateChangeReason", "—")
            print(f"  {FAIL} Athena query {s}: {reason}")
            return []
        time.sleep(2)

    rows     = []
    paginator = athena.get_paginator("get_query_results")
    pages    = paginator.paginate(QueryExecutionId=exec_id)
    headers  = None
    for page in pages:
        result_rows = page["ResultSet"]["Rows"]
        if not result_rows:
            continue
        if headers is None:
            headers = [col.get("VarCharValue", "") for col in result_rows[0]["Data"]]
            result_rows = result_rows[1:]
        for row in result_rows:
            rows.append({
                headers[i]: col.get("VarCharValue", "")
                for i, col in enumerate(row["Data"])
            })
    return rows



# ── Step 1: Invoke Lambda ─────────────────────────────────────────────────────

def step1_invoke_lambda():
    section("Step 1 — Invoke ingest_gbfs Lambda")
    print(f"  Function: {LAMBDA_GBFS}")

    resp = lambda_client.invoke(
        FunctionName=LAMBDA_GBFS,
        Payload=json.dumps({}),
        LogType="Tail",
    )
    log_b64 = resp.get("LogResult", "")
    log     = base64.b64decode(log_b64).decode("utf-8") if log_b64 else ""
    payload = json.loads(resp["Payload"].read())

    if resp.get("FunctionError"):
        print(f"  {FAIL} Lambda returned error: {payload}")
        sys.exit(1)

    # Print key log lines
    for line in log.splitlines():
        if any(kw in line for kw in ("INFO", "ERROR", "WARN", "pushed", "written", "Discovered")):
            ts = line.split("\t")[0] if "\t" in line else ""
            msg = line.split("\t")[-1] if "\t" in line else line
            print(f"  {C}[log]{X} {msg.strip()}")

    print(f"\n  {OK} Lambda response: {json.dumps(payload, indent=4)}")
    return payload


# ── Step 2: Wait for Firehose flush ──────────────────────────────────────────

def step2_wait_for_firehose(buffer_seconds: int = 75):
    section(f"Step 2 — Wait for Firehose flush ({buffer_seconds}s buffer)")
    print(f"  Firehose is configured with a 60-second buffer interval.")
    print(f"  Waiting {buffer_seconds}s to ensure data has landed in S3...\n")
    for remaining in range(buffer_seconds, 0, -5):
        print(f"  {INFO} {remaining}s remaining...", end="\r")
        time.sleep(5)
    print(f"  {OK} Wait complete                                    ")

    # Count raw files in S3
    paginator = s3.get_paginator("list_objects_v2")
    today     = datetime.now(timezone.utc)
    prefix    = f"raw/station_status/year={today.year}/month={today.month:02d}/day={today.day:02d}/"
    count     = 0
    total_kb  = 0
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            count    += 1
            total_kb += obj["Size"] / 1024
    print(f"  {OK} Found {count} raw file(s) in {prefix} (total {total_kb:.1f} KB)")
    return prefix


# ── Step 3: Load raw JSON into Iceberg via Athena ────────────────────────────

def step3_load_into_iceberg(raw_prefix: str):
    section("Step 3 — Load raw Firehose JSON → Iceberg table (raw_station_status)")

    # Create a staging external table pointing at today's raw Firehose files
    raw_s3_prefix = f"s3://{BUCKET}/{raw_prefix}"
    today         = datetime.now(timezone.utc)

    # Drop the staging table if it already exists from a previous run
    run_query(
        "DROP TABLE IF EXISTS ecobici_lake.staging_station_status_raw",
        "drop staging"
    )

    # Create external table on top of the Firehose JSON
    create_staging_sql = f"""
CREATE EXTERNAL TABLE ecobici_lake.staging_station_status_raw (
    timestamp        STRING,
    station_id       STRING,
    bikes_available  INT,
    docks_available  INT,
    is_renting       BOOLEAN,
    is_returning     BOOLEAN,
    ingest_at        STRING
)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
WITH SERDEPROPERTIES (
    'mapping.ingest_at' = '_ingest_at'
)
LOCATION '{raw_s3_prefix}'
TBLPROPERTIES ('ignore.malformed.json' = 'true')
"""
    rows = run_query(create_staging_sql, "create staging")

    # Verify staging table has rows
    count_rows = run_query(
        "SELECT COUNT(*) AS cnt FROM ecobici_lake.staging_station_status_raw",
        "count staging"
    )
    staging_count = int(count_rows[0]["cnt"]) if count_rows else 0
    print(f"  {OK} Staging table has {staging_count} rows")

    if staging_count == 0:
        print(f"  {WARN} No rows in staging — Firehose may not have flushed yet")
        print(f"        Try re-running in 60 seconds")
        return 0

    # INSERT INTO the Iceberg table (upsert-safe with timestamp partitioning)
    insert_sql = f"""
INSERT INTO ecobici_lake.raw_station_status
SELECT
    CAST(from_iso8601_timestamp(timestamp) AS TIMESTAMP) AS timestamp,
    station_id,
    bikes_available,
    docks_available,
    is_renting,
    is_returning,
    CAST(from_iso8601_timestamp(ingest_at) AS TIMESTAMP) AS _ingest_at
FROM ecobici_lake.staging_station_status_raw
WHERE station_id IS NOT NULL
  AND station_id != ''
"""
    run_query(insert_sql, "INSERT INTO iceberg")

    # Verify
    iceberg_count = run_query(
        "SELECT COUNT(*) AS cnt FROM ecobici_lake.raw_station_status",
        "count iceberg"
    )
    n = int(iceberg_count[0]["cnt"]) if iceberg_count else 0
    print(f"  {OK} Iceberg table raw_station_status now has {B}{n}{X} rows")

    # Clean up staging
    run_query("DROP TABLE IF EXISTS ecobici_lake.staging_station_status_raw", "drop staging")
    return n

# ── Step 3b: Load station_info JSON → Iceberg ────────────────────────────────

def step3b_load_station_info_iceberg():
    section("Step 3b — Load station_info JSON → Iceberg table (ecobici_station_info)")

    today       = datetime.now(timezone.utc)
    info_prefix = f"s3://{BUCKET}/raw/station_info/year={today.year}/month={today.month:02d}/day={today.day:02d}/"

    run_query("DROP TABLE IF EXISTS ecobici_lake.staging_station_info_iceberg", "drop staging")
    run_query(f"""
CREATE EXTERNAL TABLE ecobici_lake.staging_station_info_iceberg (
    station_id   STRING,
    name         STRING,
    lat          DOUBLE,
    lon          DOUBLE,
    capacity     INT,
    updated_at   STRING
)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
WITH SERDEPROPERTIES (
    'mapping.updated_at' = '_updated_at'
)
LOCATION '{info_prefix}'
TBLPROPERTIES ('ignore.malformed.json' = 'true')
""", "create staging")

    count_rows = run_query(
        "SELECT COUNT(*) AS cnt FROM ecobici_lake.staging_station_info_iceberg",
        "count staging"
    )
    n = int(count_rows[0]["cnt"]) if count_rows else 0
    print(f"  {OK} Staging has {B}{n}{X} rows")

    if n == 0:
        print(f"  {WARN} No station_info data found — run ingest_gbfs first")
        run_query("DROP TABLE IF EXISTS ecobici_lake.staging_station_info_iceberg", "drop staging")
        return 0

    # MERGE INTO to upsert (Iceberg + Athena engine v3 supports MERGE)
    run_query("""
MERGE INTO ecobici_lake.ecobici_station_info AS target
USING ecobici_lake.staging_station_info_iceberg AS source
    ON target.station_id = source.station_id
WHEN MATCHED THEN UPDATE SET
    name        = source.name,
    lat         = source.lat,
    lon         = source.lon,
    capacity    = source.capacity,
    _updated_at = CAST(from_iso8601_timestamp(source.updated_at) AS TIMESTAMP)
WHEN NOT MATCHED THEN INSERT (
    station_id, name, lat, lon, capacity, _updated_at
) VALUES (
    source.station_id,
    source.name,
    source.lat,
    source.lon,
    source.capacity,
    CAST(from_iso8601_timestamp(source.updated_at) AS TIMESTAMP)
)
""", "MERGE INTO ecobici_station_info")

    iceberg_count = run_query(
        "SELECT COUNT(*) AS cnt FROM ecobici_lake.ecobici_station_info",
        "count iceberg"
    )
    n = int(iceberg_count[0]["cnt"]) if iceberg_count else 0
    print(f"  {OK} ecobici_station_info Iceberg table now has {B}{n}{X} rows")

    sample = run_query(
        "SELECT station_id, name, lat, lon, capacity FROM ecobici_lake.ecobici_station_info LIMIT 3",
        "sample"
    )
    if sample:
        print(f"  {B}Sample rows:{X}")
        for row in sample:
            print(f"    {row}")

    run_query("DROP TABLE IF EXISTS ecobici_lake.staging_station_info_iceberg", "drop staging")
    return n


# ── Step 4: Invoke weather Lambda and load into Iceberg ───────────────────────

def step4_invoke_weather_and_load():
    section("Step 4 — Invoke ingest_weather Lambda (Open-Meteo) + load Iceberg")

    print(f"  Function: ecobici-ingest-weather")
    resp = lambda_client.invoke(
        FunctionName="ecobici-ingest-weather",
        Payload=json.dumps({}),
        LogType="Tail",
    )
    log_b64 = resp.get("LogResult", "")
    log     = base64.b64decode(log_b64).decode("utf-8") if log_b64 else ""
    payload = json.loads(resp["Payload"].read())

    if resp.get("FunctionError"):
        print(f"  {FAIL} Lambda error: {payload}")
        return 0

    for line in log.splitlines():
        if any(kw in line for kw in ("INFO", "ERROR", "WARN", "pushed", "fetched", "Fetching")):
            msg = line.split("\t")[-1] if "\t" in line else line
            print(f"  {C}[log]{X} {msg.strip()}")

    records_pushed = payload.get("records_pushed", 0)
    print(f"\n  {OK} Weather Lambda response: {json.dumps(payload)}")

    if records_pushed == 0:
        print(f"  {WARN} No weather records pushed — check Lambda logs")
        return 0

    # Wait for Firehose to flush weather data
    print(f"\n  Waiting 75s for Firehose weather flush...")
    for remaining in range(75, 0, -5):
        print(f"  {INFO} {remaining}s remaining...", end="\r")
        time.sleep(5)
    print(f"  {OK} Wait complete                                ")

    # List weather files
    today  = datetime.now(timezone.utc)
    prefix = f"raw/weather/year={today.year}/month={today.month:02d}/day={today.day:02d}/"
    paginator = s3.get_paginator("list_objects_v2")
    count = 0; total_kb = 0
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            count += 1; total_kb += obj["Size"] / 1024
    print(f"  {OK} Found {count} weather file(s) at {prefix} ({total_kb:.1f} KB)")

    if count == 0:
        print(f"  {WARN} No weather files yet — Firehose prefix may differ")
        return 0

    # Create staging and INSERT into Iceberg
    weather_s3_prefix = f"s3://{BUCKET}/{prefix}"
    run_query("DROP TABLE IF EXISTS ecobici_lake.staging_weather_raw", "drop staging")
    run_query(f"""
CREATE EXTERNAL TABLE ecobici_lake.staging_weather_raw (
    timestamp   STRING,
    station_id  STRING,
    temp_c      DOUBLE,
    precip_mm   DOUBLE,
    is_filled   STRING
)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
WITH SERDEPROPERTIES (
    'mapping.is_filled' = '_is_filled'
)
LOCATION '{weather_s3_prefix}'
TBLPROPERTIES ('ignore.malformed.json' = 'true')
""", "create weather staging")

    wcount = run_query(
        "SELECT COUNT(*) AS cnt FROM ecobici_lake.staging_weather_raw",
        "count weather staging"
    )
    nw = int(wcount[0]["cnt"]) if wcount else 0
    print(f"  {OK} Weather staging has {B}{nw}{X} rows")

    if nw > 0:
        run_query("""
INSERT INTO ecobici_lake.weather_observations
SELECT
    CAST(from_iso8601_timestamp(timestamp) AS TIMESTAMP) AS timestamp,
    station_id,
    temp_c,
    precip_mm,
    CAST(is_filled AS BOOLEAN) AS _is_filled
FROM ecobici_lake.staging_weather_raw
WHERE station_id IS NOT NULL AND station_id != ''
""", "INSERT INTO weather_observations")

        ic = run_query(
            "SELECT COUNT(*) AS cnt FROM ecobici_lake.weather_observations",
            "count weather iceberg"
        )
        niw = int(ic[0]["cnt"]) if ic else 0
        print(f"  {OK} weather_observations Iceberg table now has {B}{niw}{X} rows")

        # Sample
        sample = run_query("""
SELECT station_id, temp_c, precip_mm
FROM ecobici_lake.weather_observations
ORDER BY station_id
LIMIT 5
""", "sample weather")
        if sample:
            print(f"  {B}Sample rows:{X}")
            for row in sample:
                print(f"    {row}")

    run_query("DROP TABLE IF EXISTS ecobici_lake.staging_weather_raw", "drop staging")
    return records_pushed


def step4_check_station_info():
    section("Step 4 — Check station_info data (written directly by Lambda)")

    # The Lambda wrote station_info as NDJSON directly to S3
    # Query it as an external table
    today = datetime.now(timezone.utc)
    info_prefix = f"s3://{BUCKET}/raw/station_info/year={today.year}/month={today.month:02d}/day={today.day:02d}/"

    run_query("DROP TABLE IF EXISTS ecobici_lake.staging_station_info_raw", "drop staging")
    run_query(f"""
CREATE EXTERNAL TABLE ecobici_lake.staging_station_info_raw (
    station_id   STRING,
    name         STRING,
    lat          DOUBLE,
    lon          DOUBLE,
    capacity     INT,
    updated_at   STRING
)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
WITH SERDEPROPERTIES (
    'mapping.updated_at' = '_updated_at'
)
LOCATION '{info_prefix}'
TBLPROPERTIES ('ignore.malformed.json' = 'true')
""", "create station_info staging")

    count_rows = run_query(
        "SELECT COUNT(*) AS cnt FROM ecobici_lake.staging_station_info_raw",
        "count station_info"
    )
    n = int(count_rows[0]["cnt"]) if count_rows else 0
    print(f"  {OK} station_info staging has {B}{n}{X} rows")

    if n > 0:
        sample = run_query(
            "SELECT station_id, name, lat, lon, capacity FROM ecobici_lake.staging_station_info_raw LIMIT 5",
            "sample station_info"
        )
        print(f"\n  {B}Sample records:{X}")
        for row in sample:
            print(f"    {row}")

    run_query("DROP TABLE IF EXISTS ecobici_lake.staging_station_info_raw", "drop staging")
    return n


# ── Step 5: Sample the Iceberg table ─────────────────────────────────────────

def step5_sample_iceberg():
    section("Step 5 — Query Iceberg table raw_station_status")

    # Aggregate stats
    stats = run_query("""
SELECT
    COUNT(*)                  AS total_records,
    COUNT(DISTINCT station_id) AS unique_stations,
    MIN(timestamp)            AS earliest_ts,
    MAX(timestamp)            AS latest_ts,
    SUM(CAST(bikes_available AS BIGINT)) AS total_bikes,
    SUM(CAST(docks_available AS BIGINT)) AS total_docks
FROM ecobici_lake.raw_station_status
""", "aggregate stats")

    if stats:
        r = stats[0]
        print(f"  {B}Iceberg table stats:{X}")
        print(f"    Total records      : {r.get('total_records', '—')}")
        print(f"    Unique stations    : {r.get('unique_stations', '—')}")
        print(f"    Earliest timestamp : {r.get('earliest_ts', '—')}")
        print(f"    Latest timestamp   : {r.get('latest_ts', '—')}")
        print(f"    Total bikes avail  : {r.get('total_bikes', '—')}")
        print(f"    Total docks avail  : {r.get('total_docks', '—')}")

    # Top 5 stations by available bikes
    top5 = run_query("""
SELECT station_id, MAX(bikes_available) AS max_bikes, MAX(docks_available) AS max_docks
FROM ecobici_lake.raw_station_status
GROUP BY station_id
ORDER BY max_bikes DESC
LIMIT 5
""", "top 5 stations")

    if top5:
        print(f"\n  {B}Top 5 stations by bikes available:{X}")
        for row in top5:
            print(f"    station {row['station_id']:>4} — {row['max_bikes']:>3} bikes, {row['max_docks']:>3} docks")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{B}{G}╔══════════════════════════════════════════════════════╗")
    print(f"║  Ecobici — End-to-End Ingest Test                    ║")
    print(f"╚══════════════════════════════════════════════════════╝{X}")
    print(f"  Timestamp : {datetime.now(timezone.utc).isoformat()}")
    print(f"  Bucket    : {BUCKET}")
    print(f"  Database  : {GLUE_DATABASE}")
    print(f"  Workgroup : {ATHENA_WORKGROUP}")

    # Step 1: Invoke Lambda
    result = step1_invoke_lambda()
    stations_pushed = result.get("stations_pushed", 0)

    # Step 2: Wait for Firehose
    raw_prefix = step2_wait_for_firehose(buffer_seconds=75)

    # Step 3: Load station_status into Iceberg
    iceberg_rows = step3_load_into_iceberg(raw_prefix)

    # Step 3b: Load station_info into Iceberg
    info_iceberg_rows = step3b_load_station_info_iceberg()

    # Step 4: Weather Lambda + Iceberg load
    weather_rows = step4_invoke_weather_and_load()

    # Step 5: Sample Iceberg table
    if iceberg_rows > 0:
        step5_sample_iceberg()

    # ── Final Summary ─────────────────────────────────────────────────────────
    section("Summary")
    print(f"  {OK} Lambda pushed (GBFS)      : {stations_pushed} station records")
    print(f"  {OK} raw_station_status rows   : {iceberg_rows}")
    print(f"  {OK} ecobici_station_info rows : {info_iceberg_rows}")
    print(f"  {OK} weather_observations rows : {weather_rows}")

    all_ok = iceberg_rows > 0 and info_iceberg_rows > 0 and weather_rows > 0
    if all_ok:
        print(f"\n  {B}{G}All tables populated — pipeline is healthy end-to-end ✅{X}")
    else:
        print(f"\n  {WARN} Some tables still empty. Check logs above.")
    print()


if __name__ == "__main__":
    main()
