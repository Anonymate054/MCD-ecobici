"""
process_historical_status.py — Reconstruct Historical Station Status from Trips
=============================================================================
Triggered automatically after ingest_trips finishes loading a new month of trips.

This Lambda:
  1. Ensures target Iceberg tables exist.
  2. Pulls station capacities from ecobici_station_info.
  3. Pulls 15-minute checkout/checkin counts for the target month from trips.
  4. Runs a bounded flow simulation to estimate available bikes and docks.
  5. Saves results to S3, loads them into historical_station_status_15m.
  6. Aggregates to 1 hour and joins weather, inserting into historical_station_status_1h.
"""

import csv
import io
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------
athena_client = boto3.client("athena")
s3_client     = boto3.client("s3")
glue_client   = boto3.client("glue")

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
GLUE_DATABASE    = os.environ["GLUE_DATABASE"]
ATHENA_WORKGROUP = os.environ["ATHENA_WORKGROUP"]
S3_BUCKET        = os.environ["S3_BUCKET"]

RESULTS_PREFIX   = f"s3://{S3_BUCKET}/athena-results/"
POLL_INTERVAL    = 3
MAX_WAIT_SECS    = 400

# ---------------------------------------------------------------------------
# Athena Helpers
# ---------------------------------------------------------------------------

def _run_query_get_key(sql: str, description: str) -> str:
    """Submit an Athena query, wait for it, and return the output CSV S3 key."""
    logger.info("Athena [%s]: submitting...", description)
    resp = athena_client.start_query_execution(
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
            break

        if state in ("FAILED", "CANCELLED"):
            reason = status.get("StateChangeReason", "—")
            raise RuntimeError(f"Athena [{description}] {state}: {reason}")
    else:
        raise TimeoutError(f"Athena [{description}] timed out after {MAX_WAIT_SECS}s")

    return f"athena-results/{exec_id}.csv"


def _run_query_dml(sql: str, description: str) -> None:
    """Submit a DML/DDL Athena query and block until completion."""
    _run_query_get_key(sql, description)


# ---------------------------------------------------------------------------
# Core Logic
# ---------------------------------------------------------------------------

def handler(event: Any, context: Any) -> dict:
    req_id = context.aws_request_id.replace("-", "_")
    
    # Parse target month
    year = event.get("year")
    month = event.get("month")
    
    if not year or not month:
        # Fallback to checking payload variants or use previous month
        ym = event.get("ym", "")
        if ym and "-" in ym:
            year, month = ym.split("-")
        else:
            raise ValueError("Event must contain 'year' and 'month' or 'ym'")
            
    year_int = int(year)
    month_int = int(month)
    
    start_time = f"{year_int:04d}-{month_int:02d}-01 00:00:00"
    if month_int == 12:
        next_year = year_int + 1
        next_month = 1
    else:
        next_year = year_int
        next_month = month_int + 1
    end_time = f"{next_year:04d}-{next_month:02d}-01 00:00:00"
    
    logger.info("Starting historical status reconstruction for %d-%02d (%s to %s)", 
                year_int, month_int, start_time, end_time)

    # 1. Ensure Iceberg tables exist
    _run_query_dml(f"""
    CREATE TABLE IF NOT EXISTS historical_station_status_15m (
        timestamp                 TIMESTAMP,
        station_id                STRING,
        checkouts                 INT,
        checkins                  INT,
        net_delta                 INT,
        estimated_bikes_available DOUBLE,
        estimated_docks_available DOUBLE,
        capacity                  INT,
        station_state             STRING
    )
    PARTITIONED BY (month(timestamp))
    LOCATION 's3://{S3_BUCKET}/processed/historical_station_status_15m/'
    TBLPROPERTIES (
        'table_type'='ICEBERG',
        'format'='parquet',
        'write_compression'='snappy'
    )
    """, "Ensure 15m target table")

    _run_query_dml(f"""
    CREATE TABLE IF NOT EXISTS historical_station_status_1h (
        hour                      TIMESTAMP,
        station_id                STRING,
        checkouts                 INT,
        checkins                  INT,
        net_delta                 INT,
        estimated_bikes_available DOUBLE,
        estimated_docks_available DOUBLE,
        capacity                  INT,
        temp_c                    DOUBLE,
        precip_mm                 DOUBLE,
        station_state             STRING
    )
    PARTITIONED BY (month(hour))
    LOCATION 's3://{S3_BUCKET}/processed/historical_station_status_1h/'
    TBLPROPERTIES (
        'table_type'='ICEBERG',
        'format'='parquet',
        'write_compression'='snappy'
    )
    """, "Ensure 1h target table")

    # 2. Get latest station capacities
    cap_sql = """
    SELECT station_id, MAX(capacity) AS capacity 
    FROM ecobici_station_info 
    GROUP BY station_id
    """
    cap_s3_key = _run_query_get_key(cap_sql, "Get station capacities")
    
    # Download and parse capacities
    cap_obj = s3_client.get_object(Bucket=S3_BUCKET, Key=cap_s3_key)
    cap_lines = cap_obj["Body"].read().decode("utf-8").splitlines()
    capacities = {}
    for row in csv.DictReader(cap_lines):
        st_id = row.get("station_id")
        cap = row.get("capacity")
        if st_id and cap:
            try:
                capacities[st_id] = int(cap)
            except ValueError:
                pass
    logger.info("Loaded %d station capacities", len(capacities))

    # 3. Pull 15-minute aggregated checkins and checkouts
    flow_sql = f"""
    WITH intervals AS (
        SELECT
            date_add('minute', -(minute(start_timestamp) % 15), date_trunc('minute', start_timestamp)) AS interval_start,
            start_station_id AS station_id,
            count(*) AS checkouts,
            0 AS checkins
        FROM trips
        WHERE start_timestamp >= TIMESTAMP '{start_time}' AND start_timestamp < TIMESTAMP '{end_time}'
        GROUP BY 1, 2
        
        UNION ALL
        
        SELECT
            date_add('minute', -(minute(end_timestamp) % 15), date_trunc('minute', end_timestamp)) AS interval_start,
            end_station_id AS station_id,
            0 AS checkouts,
            count(*) AS checkins
        FROM trips
        WHERE end_timestamp >= TIMESTAMP '{start_time}' AND end_timestamp < TIMESTAMP '{end_time}'
        GROUP BY 1, 2
    )
    SELECT
        date_format(interval_start, '%Y-%m-%d %H:%i:%s') AS timestamp,
        station_id,
        CAST(sum(checkouts) AS INT) AS checkouts,
        CAST(sum(checkins) AS INT) AS checkins
    FROM intervals
    GROUP BY 1, 2
    ORDER BY 1, 2
    """
    flow_s3_key = _run_query_get_key(flow_sql, "Get monthly 15m flows")

    # Download and parse flows
    flow_obj = s3_client.get_object(Bucket=S3_BUCKET, Key=flow_s3_key)
    flow_lines = flow_obj["Body"].read().decode("utf-8").splitlines()
    flows = {}
    for row in csv.DictReader(flow_lines):
        ts = row.get("timestamp")
        st_id = row.get("station_id")
        checkouts = int(row.get("checkouts") or 0)
        checkins = int(row.get("checkins") or 0)
        
        if st_id not in flows:
            flows[st_id] = {}
        flows[st_id][ts] = (checkouts, checkins)
        
    logger.info("Loaded flows for %d stations", len(flows))

    # Fill default capacities for any station seen in flows but not capacities
    for st_id in flows:
        if st_id not in capacities:
            capacities[st_id] = 15  # default fallback capacity

    # 4. Generate the full time slots grid
    start_dt = datetime(year_int, month_int, 1, 0, 0, 0)
    if month_int == 12:
        end_dt = datetime(year_int + 1, 1, 1, 0, 0, 0)
    else:
        end_dt = datetime(year_int, month_int + 1, 1, 0, 0, 0)
        
    time_slots = []
    curr = start_dt
    while curr < end_dt:
        time_slots.append(curr.strftime("%Y-%m-%d %H:%M:%S"))
        curr += timedelta(minutes=15)

    # 5. Bounded Simulation
    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow([
        "timestamp", "station_id", "checkouts", "checkins", 
        "net_delta", "estimated_bikes_available", "estimated_docks_available", "capacity", "station_state"
    ])

    total_records = 0
    for st_id, cap in capacities.items():
        st_flows = flows.get(st_id, {})
        bikes = int(cap * 0.5)  # Initialize with 50% capacity
        
        for slot in time_slots:
            checkouts, checkins = st_flows.get(slot, (0, 0))
            
            # Determine state based on starting bikes for this slot
            state = "NORMAL"
            if bikes == 0:
                if checkouts > 0:
                    state = "REBALANCED_REFILL"
                else:
                    state = "STARVED"
            elif bikes >= cap:
                if checkins > 0:
                    state = "REBALANCED_DEPLETE"
                else:
                    state = "OVERFLOW"
            
            # Bounded update
            bikes = max(0, min(cap, bikes + checkins - checkouts))
            docks = cap - bikes
            net_delta = checkins - checkouts
            
            writer.writerow([
                slot, st_id, checkouts, checkins, net_delta, 
                float(bikes), float(docks), cap, state
            ])
            total_records += 1

    # Upload simulation CSV to S3 temp location
    temp_key = f"tmp/staging_historical_15m/{year}_{month}_{req_id}.csv"
    logger.info("Uploading %d simulated status rows to s3://%s/%s", total_records, S3_BUCKET, temp_key)
    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=temp_key,
        Body=csv_buffer.getvalue().encode("utf-8"),
        ContentType="text/csv"
    )

    # 6. Load into historical_station_status_15m via staging table
    staging_tbl = f"staging_historical_15m_{year}_{month}_{req_id}"
    try:
        # Create Glue staging table
        glue_client.create_table(
            DatabaseName=GLUE_DATABASE,
            TableInput={
                'Name': staging_tbl,
                'TableType': 'EXTERNAL_TABLE',
                'Parameters': {
                    'classification': 'csv',
                    'skip.header.line.count': '1'
                },
                'StorageDescriptor': {
                    'Columns': [
                        {'Name': 'timestamp', 'Type': 'string'},
                        {'Name': 'station_id', 'Type': 'string'},
                        {'Name': 'checkouts', 'Type': 'string'},
                        {'Name': 'checkins', 'Type': 'string'},
                        {'Name': 'net_delta', 'Type': 'string'},
                        {'Name': 'estimated_bikes_available', 'Type': 'string'},
                        {'Name': 'estimated_docks_available', 'Type': 'string'},
                        {'Name': 'capacity', 'Type': 'string'},
                        {'Name': 'station_state', 'Type': 'string'}
                    ],
                    'Location': f's3://{S3_BUCKET}/tmp/staging_historical_15m/',
                    'InputFormat': 'org.apache.hadoop.mapred.TextInputFormat',
                    'OutputFormat': 'org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat',
                    'SerdeInfo': {
                        'SerializationLibrary': 'org.apache.hadoop.hive.serde2.OpenCSVSerde',
                        'Parameters': {
                            'separatorChar': ',',
                            'quoteChar': '"'
                        }
                    }
                }
            }
        )

        # INSERT INTO 15m Target Table
        insert_15m_sql = f"""
        INSERT INTO historical_station_status_15m
        SELECT
            CAST(timestamp AS TIMESTAMP) AS timestamp,
            station_id,
            CAST(checkouts AS INT) AS checkouts,
            CAST(checkins AS INT) AS checkins,
            CAST(net_delta AS INT) AS net_delta,
            CAST(estimated_bikes_available AS DOUBLE) AS estimated_bikes_available,
            CAST(estimated_docks_available AS DOUBLE) AS estimated_docks_available,
            CAST(capacity AS INT) AS capacity,
            station_state
        FROM {staging_tbl}
        WHERE timestamp IS NOT NULL AND station_id IS NOT NULL
        """
        _run_query_dml(insert_15m_sql, "Insert staging to target 15m")

        # 7. Aggregate and Join Weather, INSERT into target 1h
        insert_1h_sql = f"""
        WITH hourly_agg AS (
            SELECT
                date_trunc('hour', timestamp) AS hour,
                station_id,
                CAST(SUM(checkouts) AS INT) AS checkouts,
                CAST(SUM(checkins) AS INT) AS checkins,
                CAST(SUM(net_delta) AS INT) AS net_delta,
                ROUND(AVG(estimated_bikes_available), 2) AS estimated_bikes_available,
                ROUND(AVG(estimated_docks_available), 2) AS estimated_docks_available,
                MAX(capacity) AS capacity,
                CASE 
                    WHEN SUM(CASE WHEN station_state = 'REBALANCED_REFILL' THEN 1 ELSE 0 END) > 0 THEN 'REBALANCED_REFILL'
                    WHEN SUM(CASE WHEN station_state = 'REBALANCED_DEPLETE' THEN 1 ELSE 0 END) > 0 THEN 'REBALANCED_DEPLETE'
                    WHEN SUM(CASE WHEN station_state = 'STARVED' THEN 1 ELSE 0 END) > 0 THEN 'STARVED'
                    WHEN SUM(CASE WHEN station_state = 'OVERFLOW' THEN 1 ELSE 0 END) > 0 THEN 'OVERFLOW'
                    ELSE 'NORMAL'
                END AS station_state
            FROM historical_station_status_15m
            WHERE timestamp >= TIMESTAMP '{start_time}' AND timestamp < TIMESTAMP '{end_time}'
            GROUP BY 1, 2
        ),
        weather AS (
            SELECT
                date_trunc('hour', "timestamp") AS hour,
                station_id,
                AVG(temp_c)    AS temp_c,
                SUM(precip_mm) AS precip_mm
            FROM weather_observations
            WHERE _is_filled = FALSE
            GROUP BY 1, 2
        ),
        joined AS (
            SELECT
                h.hour,
                h.station_id,
                h.checkouts,
                h.checkins,
                h.net_delta,
                h.estimated_bikes_available,
                h.estimated_docks_available,
                h.capacity,
                w.temp_c,
                w.precip_mm,
                h.station_state
            FROM hourly_agg h
            LEFT JOIN vw_ecobici_weather_mapping vm
                ON vm.ecobici_station_id = h.station_id
            LEFT JOIN weather w
                ON  w.hour       = h.hour
                AND w.station_id = vm.weather_station_id
        )
        INSERT INTO historical_station_status_1h
        SELECT
            hour,
            station_id,
            checkouts,
            checkins,
            net_delta,
            estimated_bikes_available,
            estimated_docks_available,
            capacity,
            COALESCE(
                temp_c,
                LAG(temp_c) OVER (PARTITION BY station_id ORDER BY hour)
            ) AS temp_c,
            COALESCE(precip_mm, 0.0) AS precip_mm,
            station_state
        FROM joined
        ORDER BY hour, station_id
        """
        _run_query_dml(insert_1h_sql, "Insert target 15m to target 1h (with weather)")

        logger.info("Successfully reconstructed 15m and 1h historical tables for %d-%02d", year_int, month_int)

    finally:
        # Cleanup
        logger.info("Cleaning up staging table and files...")
        try:
            glue_client.delete_table(DatabaseName=GLUE_DATABASE, Name=staging_tbl)
        except Exception as e:
            logger.warning("Staging table cleanup failed: %s", e)
            
        try:
            s3_client.delete_object(Bucket=S3_BUCKET, Key=temp_key)
        except Exception as e:
            logger.warning("S3 staging file cleanup failed: %s", e)

    return {
        "statusCode": 200,
        "body": f"Historical status reconstruction for {year}-{month} completed successfully."
    }
