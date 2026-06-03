"""
ingest_trips.py — Historical and Incremental Ecobici Trips Ingestion
=====================================================================
This script automates the ingestion of monthly Ecobici trip datasets into
the Iceberg table `ecobici_lake.trips`.

Workflow:
  1. Checks if the `ecobici_lake.trips` table exists in Athena. If not, it
     creates it, partitioned by month on the `start_timestamp` column.
  2. Scrapes the official Ecobici open data portal (https://ecobici.cdmx.gob.mx/datos-abiertos/)
     to discover all published monthly CSV links.
  3. Queries Athena to see which months (e.g., '2023-01', '2023-02') are already
     loaded in the `trips` Iceberg table.
  4. For any month from the start-month (default 2023-01) to the present that is
     available on the portal but not yet in Athena:
       a. Downloads the CSV file.
       b. Cleans and normalizes columns, renaming them to English equivalents.
       c. Formats timestamps cleanly.
       d. Writes a temporary Parquet file and uploads it to S3 staging.
       e. Creates a staging table in Athena and runs `INSERT INTO trips` to append.
       f. Drops staging table and cleans up S3.

Usage:
    python ingest_trips.py --start-month 2023-01 --aws-profile ecobici-de-01
"""

import argparse
import io
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
import urllib.request

import boto3
import pandas as pd
from bs4 import BeautifulSoup

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

PORTAL_URL = "https://ecobici.cdmx.gob.mx/datos-abiertos/"

# ---------------------------------------------------------------------------
# Athena Helpers
# ---------------------------------------------------------------------------

def run_athena_query(athena_client, db_name, workgroup, results_bucket, sql, description):
    """Run an Athena query and return execution details and result rows."""
    logger.info("Submitting query [%s]...", description)
    results_path = f"s3://{results_bucket}/athena-results/"
    
    resp = athena_client.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": db_name},
        ResultConfiguration={"OutputLocation": results_path},
        WorkGroup=workgroup,
    )
    exec_id = resp["QueryExecutionId"]
    
    # Poll status
    while True:
        status_resp = athena_client.get_query_execution(QueryExecutionId=exec_id)
        state = status_resp["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = status_resp["QueryExecution"]["Status"].get("StateChangeReason", "Unknown")
            raise RuntimeError(f"Query '{description}' failed: {reason}")
        time.sleep(2)
        
    # Fetch results
    try:
        rows = []
        pager = athena_client.get_paginator("get_query_results")
        headers = None
        for page in pager.paginate(QueryExecutionId=exec_id):
            result_rows = page["ResultSet"]["Rows"]
            if not result_rows:
                continue
            if headers is None:
                headers = [c.get("VarCharValue", "") for c in result_rows[0]["Data"]]
                result_rows = result_rows[1:]
            for r in result_rows:
                rows.append({headers[i]: col.get("VarCharValue", "") for i, col in enumerate(r["Data"])})
        return rows
    except Exception as e:
        # DDL / DML queries (like INSERT or CREATE) might not return results
        return []

# ---------------------------------------------------------------------------
# Scraping helper
# ---------------------------------------------------------------------------

def discover_portal_csvs():
    """Scrape the Ecobici website to map YYYY-MM -> CSV URL."""
    import urllib.parse
    logger.info("Scraping %s to discover trip CSVs...", PORTAL_URL)
    req = urllib.request.Request(
        PORTAL_URL, 
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        html = response.read()
    
    soup = BeautifulSoup(html, "html.parser")
    csv_links = {}
    
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.endswith(".csv"):
            full_url = urllib.parse.urljoin(PORTAL_URL, href)
            # Extract YYYY-MM from the filename/link
            # Example filename: /2026/02/2026-01.csv or OD_202301.csv
            match = re.search(r"(\d{4})[-_]?(\d{2})", full_url)
            if match:
                year, month = match.groups()
                # Verify it is a valid year range (e.g. 2010 to 2030)
                if 2010 <= int(year) <= 2030 and 1 <= int(month) <= 12:
                    key = f"{year}-{month}"
                    csv_links[key] = full_url
                    
    logger.info("Discovered %d monthly CSV files on the portal.", len(csv_links))
    return csv_links

# ---------------------------------------------------------------------------
# Schema setup
# ---------------------------------------------------------------------------

def ensure_trips_table(athena_client, db_name, workgroup, bucket):
    """Ensure the partitioned Iceberg trips table exists."""
    sql = f"""
    CREATE TABLE IF NOT EXISTS {db_name}.trips (
        user_gender       STRING,
        user_age          INT,
        bike_id           STRING,
        start_station_id  STRING,
        end_station_id    STRING,
        start_timestamp   TIMESTAMP,
        end_timestamp     TIMESTAMP
    )
    PARTITIONED BY (month(start_timestamp))
    LOCATION 's3://{bucket}/processed/trips/'
    TBLPROPERTIES (
        'table_type'='ICEBERG',
        'format'='parquet',
        'write_compression'='snappy'
    )
    """
    run_athena_query(athena_client, db_name, workgroup, bucket, sql, "Ensure trips table")

# ---------------------------------------------------------------------------
# Data Cleaning & Normalization
# ---------------------------------------------------------------------------

def download_and_clean_csv(url, year_str, month_str):
    """Download, clean, and map columns to English equivalents."""
    logger.info("Downloading %s ...", url)
    req = urllib.request.Request(
        url, 
        headers={'User-Agent': 'Mozilla/5.0'}
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        content = response.read()
        
    df = None
    for enc in ("utf-8-sig", "latin-1", "utf-8"):
        try:
            df = pd.read_csv(io.StringIO(content.decode(enc)), low_memory=False)
            logger.info("Decoded successfully with %s", enc)
            break
        except Exception:
            continue
            
    if df is None:
        raise ValueError("Could not decode CSV with latin-1, utf-8, or utf-8-sig")

    logger.info("Original row count: %d", len(df))

    # Normalize column headers
    df.columns = df.columns.str.strip().str.replace(r'\s+', '_', regex=True)
    col_map = {c.lower().replace("_", ""): c for c in df.columns}

    # Find the corresponding original column names
    gender_col = next((col_map[k] for k in ["generousuario", "gender", "genero"] if k in col_map), None)
    age_col = next((col_map[k] for k in ["edadusuario", "age", "edad"] if k in col_map), None)
    bike_col = next((col_map[k] for k in ["bici", "bikeid", "idbici"] if k in col_map), None)
    
    start_st_col = next((col_map[k] for k in ["cicloestacionretiro", "startstationid", "estacionretiro"] if k in col_map), None)
    end_st_col = next((col_map[k] for k in ["cicloestacionarribo", "endstationid", "estacionarribo"] if k in col_map), None)
    
    start_date_col = next((col_map[k] for k in ["fecharetiro", "startdate"] if k in col_map), None)
    start_time_col = next((col_map[k] for k in ["horaretiro", "starttime"] if k in col_map), None)
    end_date_col = next((col_map[k] for k in ["fechaarribo", "enddate"] if k in col_map), None)
    end_time_col = next((col_map[k] for k in ["horaarribo", "endtime"] if k in col_map), None)

    # Validate essential columns
    missing = []
    for var, col in [
        ("start_station", start_st_col), ("end_station", end_st_col),
        ("start_date", start_date_col), ("start_time", start_time_col),
        ("end_date", end_date_col), ("end_time", end_time_col)
    ]:
        if col is None:
            missing.append(var)
    if missing:
        raise ValueError(f"Missing required columns mapping for: {missing}")

    # Build the clean DataFrame
    clean_df = pd.DataFrame()
    
    # 1. Demographics
    if gender_col:
        clean_df["user_gender"] = df[gender_col].astype(str).str.strip().str.upper().apply(
            lambda x: x if x in ("M", "F") else None
        )
    else:
        clean_df["user_gender"] = None
        
    if age_col:
        clean_df["user_age"] = pd.to_numeric(df[age_col], errors="coerce").astype("Int64")
    else:
        clean_df["user_age"] = None

    # 2. Identifiers
    if bike_col:
        clean_df["bike_id"] = df[bike_col].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
    else:
        clean_df["bike_id"] = None

    # Normalization helper for station IDs
    def normalize_station_id(series):
        return pd.to_numeric(series, errors="coerce").fillna(-1).astype(int).astype(str).replace("-1", None)

    clean_df["start_station_id"] = normalize_station_id(df[start_st_col])
    clean_df["end_station_id"] = normalize_station_id(df[end_st_col])

    # 3. Timestamps
    def parse_datetime(date_series, time_series):
        combined = date_series.astype(str).str.strip() + " " + time_series.astype(str).str.strip()
        # format: supports DD/MM/YYYY or YYYY-MM-DD
        return pd.to_datetime(combined, dayfirst=True, errors="coerce")

    clean_df["start_timestamp"] = parse_datetime(df[start_date_col], df[start_time_col])
    clean_df["end_timestamp"] = parse_datetime(df[end_date_col], df[end_time_col])

    # Drop records with invalid timestamps or missing stations
    clean_df = clean_df.dropna(subset=["start_timestamp", "end_timestamp", "start_station_id", "end_station_id"])
    logger.info("Cleaned row count: %d", len(clean_df))

    return clean_df

# ---------------------------------------------------------------------------
# Main Process
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Ingest Ecobici monthly trip files into Iceberg.")
    parser.add_argument("--start-month", default="2023-01", help="Ingest data starting from this month (YYYY-MM)")
    parser.add_argument("--aws-profile", default=None, help="AWS CLI profile name")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.aws_profile)
    athena_client = session.client("athena", region_name="us-east-1")
    s3_client = session.client("s3", region_name="us-east-1")
    
    # Get config variables
    db_name = "ecobici_lake"
    workgroup = "ecobici-workgroup"
    bucket = "ecobici-datalake-195202617652"

    logger.info("Initializing trips ingestion pipeline...")
    ensure_trips_table(athena_client, db_name, workgroup, bucket)

    # Scrape published CSVs
    discovered_csvs = discover_portal_csvs()
    
    # Query loaded months from the Iceberg table
    loaded_months = set()
    try:
        loaded_res = run_athena_query(
            athena_client, db_name, workgroup, bucket,
            "SELECT DISTINCT date_format(start_timestamp, '%Y-%m') as ym FROM trips",
            "Get loaded months"
        )
        loaded_months = {r["ym"] for r in loaded_res if r.get("ym")}
        logger.info("Months already ingested in Iceberg: %s", sorted(list(loaded_months)))
    except Exception as e:
        logger.warning("Could not query loaded months (might be empty): %s", e)

    # Filter target months (>= start_month)
    target_months = sorted([
        ym for ym in discovered_csvs.keys() 
        if ym >= args.start_month and ym not in loaded_months
    ])

    if not target_months:
        logger.info("All months up to date. No new datasets to ingest.")
        return

    logger.info("New months to ingest: %s", target_months)

    for ym in target_months:
        url = discovered_csvs[ym]
        year, month = ym.split("-")
        
        logger.info("--- Starting Ingestion: %s ---", ym)
        
        try:
            # 1. Download & Clean
            clean_df = download_and_clean_csv(url, year, month)
            
            # 2. Upload staging Parquet file to S3
            staging_key = f"tmp/staging_trips/year={year}/month={month}/staging.parquet"
            logger.info("Uploading cleaned Parquet to staging path: s3://%s/%s", bucket, staging_key)
            
            pq_buffer = io.BytesIO()
            clean_df.to_parquet(pq_buffer, index=False, engine="pyarrow", compression="snappy")
            pq_buffer.seek(0)
            
            s3_client.put_object(
                Bucket=bucket,
                Key=staging_key,
                Body=pq_buffer.getvalue(),
                ContentType="application/octet-stream",
            )
            
            # 3. Create Athena staging table over S3 parquet path
            staging_tbl = f"staging_trips_{year}_{month}"
            
            # Drop staging table if left over
            run_athena_query(
                athena_client, db_name, workgroup, bucket,
                f"DROP TABLE IF EXISTS {staging_tbl}", 
                f"Drop staging table {staging_tbl}"
            )
            
            # Create staging external table
            run_athena_query(
                athena_client, db_name, workgroup, bucket,
                f"""
                CREATE EXTERNAL TABLE {staging_tbl} (
                    user_gender       STRING,
                    user_age          INT,
                    bike_id           STRING,
                    start_station_id  STRING,
                    end_station_id    STRING,
                    start_timestamp   TIMESTAMP,
                    end_timestamp     TIMESTAMP
                )
                STORED AS PARQUET
                LOCATION 's3://{bucket}/tmp/staging_trips/year={year}/month={month}/'
                """, 
                f"Create staging table {staging_tbl}"
            )
            
            # 4. Insert from staging to Iceberg trips table
            # Athena Iceberg handles partitioning automatically
            run_athena_query(
                athena_client, db_name, workgroup, bucket,
                f"""
                INSERT INTO trips
                SELECT 
                    user_gender,
                    user_age,
                    bike_id,
                    start_station_id,
                    end_station_id,
                    start_timestamp,
                    end_timestamp
                FROM {staging_tbl}
                """,
                f"Insert staging {ym} into Iceberg trips"
            )
            
            # 5. Cleanup
            logger.info("Cleaning up staging tables and files...")
            run_athena_query(
                athena_client, db_name, workgroup, bucket,
                f"DROP TABLE IF EXISTS {staging_tbl}",
                f"Cleanup drop staging table {staging_tbl}"
            )
            
            s3_client.delete_object(Bucket=bucket, Key=staging_key)
            logger.info("✅ Month %s successfully ingested into trips table.", ym)
            
        except Exception as e:
            logger.error("❌ Failed to ingest month %s: %s", ym, e, exc_info=True)
            # Make sure we clean up if failed
            try:
                run_athena_query(
                    athena_client, db_name, workgroup, bucket,
                    f"DROP TABLE IF EXISTS staging_trips_{year}_{month}",
                    "Error cleanup drop table"
                )
                s3_client.delete_object(Bucket=bucket, Key=f"tmp/staging_trips/year={year}/month={month}/staging.parquet")
            except Exception:
                pass
            sys.exit(1)

    logger.info("All targeted months completed successfully.")

if __name__ == "__main__":
    main()
