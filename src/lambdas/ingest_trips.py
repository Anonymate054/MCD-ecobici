"""
ingest_trips.py — Serverless Monthly Trips Ingestion Lambda
===========================================================
This Lambda automatically discovers, downloads, and processes monthly Ecobici 
trip datasets. It uses Athena to perform the ETL, avoiding large memory and 
CPU usage in the Lambda container.

Triggers: Scheduled EventBridge rule (e.g. daily/weekly).
"""

import os
import re
import sys
import time
import csv
import logging
import urllib.request
import urllib.parse
from html.parser import HTMLParser
import boto3

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# HTML Parser to discover links
# ---------------------------------------------------------------------------

class EcobiciLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = {}

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            attrs_dict = dict(attrs)
            href = attrs_dict.get('href', '')
            if href.endswith('.csv'):
                match = re.search(r"(\d{4})[-_]?(\d{2})", href)
                if match:
                    year, month = match.groups()
                    if 2010 <= int(year) <= 2030 and 1 <= int(month) <= 12:
                        key = f"{year}-{month}"
                        self.links[key] = href

# ---------------------------------------------------------------------------
# Athena Helper
# ---------------------------------------------------------------------------

def run_athena_query(athena_client, db_name, workgroup, results_bucket, sql, description):
    logger.info("Submitting Athena query [%s]...", description)
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
    except Exception:
        return []

# ---------------------------------------------------------------------------
# Main Handler
# ---------------------------------------------------------------------------

def handler(event, context):
    db_name = os.environ["GLUE_DATABASE"]
    workgroup = os.environ["ATHENA_WORKGROUP"]
    bucket = os.environ["S3_BUCKET"]
    start_month = os.environ.get("START_MONTH", "2023-01")

    session = boto3.Session()
    athena_client = session.client("athena")
    s3_client = session.client("s3")

    # 1. Ensure Iceberg trips table exists
    trips_ddl = f"""
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
    run_athena_query(athena_client, db_name, workgroup, bucket, trips_ddl, "Ensure Iceberg trips table")

    # 2. Scrape portal page to discover CSV URLs
    portal_url = "https://ecobici.cdmx.gob.mx/datos-abiertos/"
    logger.info("Scraping portal for links...")
    req = urllib.request.Request(portal_url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30) as response:
        html = response.read().decode('utf-8')
    
    link_parser = EcobiciLinkParser()
    link_parser.feed(html)
    discovered_csvs = link_parser.links
    logger.info("Discovered %d monthly CSV files on the portal.", len(discovered_csvs))

    # 3. Query already loaded months in Iceberg table
    loaded_months = set()
    try:
        loaded_res = run_athena_query(
            athena_client, db_name, workgroup, bucket,
            "SELECT DISTINCT date_format(start_timestamp, '%Y-%m') as ym FROM trips",
            "Get loaded months"
        )
        loaded_months = {r["ym"] for r in loaded_res if r.get("ym")}
        logger.info("Months already ingested: %s", sorted(list(loaded_months)))
    except Exception as e:
        logger.warning("Could not query loaded months: %s", e)

    # 4. Filter target months (>= start_month and not loaded)
    target_months = sorted([
        ym for ym in discovered_csvs.keys() 
        if ym >= start_month and ym not in loaded_months
    ])

    if not target_months:
        logger.info("All months up to date. No new datasets to ingest.")
        return {
            "statusCode": 200,
            "body": "All months up to date."
        }

    # Process oldest target month first
    target_ym = target_months[0]
    url = urllib.parse.urljoin(portal_url, discovered_csvs[target_ym])
    year, month = target_ym.split("-")
    logger.info("Processing target month: %s from %s", target_ym, url)

    req_id = context.aws_request_id.replace("-", "_")

    # 5. Stream CSV from website to raw staging in S3
    raw_s3_key = f"tmp/staging_trips_raw/year={year}/month={month}/{req_id}_trips.csv"
    logger.info("Streaming CSV to s3://%s/%s ...", bucket, raw_s3_key)
    
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=120) as response:
        s3_client.upload_fileobj(response, bucket, raw_s3_key)
        
    logger.info("Successfully uploaded raw CSV to S3.")

    # 6. Read first few bytes to discover column indices dynamically
    resp = s3_client.get_object(Bucket=bucket, Key=raw_s3_key, Range="bytes=0-10000")
    first_bytes = resp["Body"].read()
    header_line = ""
    for enc in ("utf-8-sig", "latin-1", "utf-8"):
        try:
            lines = first_bytes.decode(enc).splitlines()
            if lines:
                header_line = lines[0]
                break
        except Exception:
            continue
    if not header_line:
        raise ValueError("Could not parse CSV headers from uploaded file.")

    headers = [h.strip().lower().replace("_", "").replace(" ", "") for h in list(csv.reader([header_line]))[0]]
    logger.info("Normalized headers: %s", headers)

    # Map column names to indices
    def find_index(options):
        for opt in options:
            if opt in headers:
                return headers.index(opt)
        return None

    gender_idx = find_index(["generousuario", "gender", "genero"])
    age_idx = find_index(["edadusuario", "age", "edad"])
    bike_idx = find_index(["bici", "bikeid", "idbici"])
    start_st_idx = find_index(["cicloestacionretiro", "startstationid", "estacionretiro"])
    start_date_idx = find_index(["fecharetiro", "startdate"])
    start_time_idx = find_index(["horaretiro", "starttime"])
    end_st_idx = find_index(["cicloestacionarribo", "cicloestacionarribo", "endstationid", "estacionarribo"])
    end_date_idx = find_index(["fechaarribo", "enddate"])
    end_time_idx = find_index(["horaarribo", "endtime"])

    # Build Athena staging table columns
    num_cols = len(headers)
    staging_cols_def = ", ".join([f"c_{i} STRING" for i in range(num_cols)])
    staging_tbl = f"staging_trips_csv_{year}_{month}_{req_id}"

    glue_client = session.client("glue")
    try:
        # 7. Create staging table over raw CSV using Glue Client directly
        try:
            glue_client.delete_table(DatabaseName=db_name, Name=staging_tbl)
            logger.info("Dropped old staging table %s", staging_tbl)
        except Exception:
            pass

        logger.info("Creating staging table %s via Glue catalog API...", staging_tbl)
        glue_client.create_table(
            DatabaseName=db_name,
            TableInput={
                'Name': staging_tbl,
                'TableType': 'EXTERNAL_TABLE',
                'Parameters': {
                    'classification': 'csv',
                    'skip.header.line.count': '1'
                },
                'StorageDescriptor': {
                    'Columns': [{'Name': f'c_{i}', 'Type': 'string'} for i in range(num_cols)],
                    'Location': f's3://{bucket}/tmp/staging_trips_raw/year={year}/month={month}/',
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

        # Helper to format column select statement
        def col_sel(idx, transform=""):
            if idx is None:
                return "CAST(NULL AS STRING)"
            val = f"trim(c_{idx})"
            if transform == "gender":
                return f"nullif(upper({val}), '')"
            if transform == "age":
                return f"try_cast({val} as INT)"
            return f"nullif({val}, '')"

        def dt_sel(date_idx, time_idx):
            if date_idx is None or time_idx is None:
                return "CAST(NULL AS TIMESTAMP)"
            val = f"concat(trim(c_{date_idx}), ' ', trim(c_{time_idx}))"
            return f"coalesce(try(date_parse({val}, '%d/%m/%Y %H:%i:%s')), try(date_parse({val}, '%Y-%m-%d %H:%i:%s')))"

        # Build dynamic SELECT fields
        gender_field = col_sel(gender_idx, "gender")
        age_field = col_sel(age_idx, "age")
        bike_field = col_sel(bike_idx)
        start_st_field = col_sel(start_st_idx)
        end_st_field = col_sel(end_st_idx)
        start_ts_field = dt_sel(start_date_idx, start_time_idx)
        end_ts_field = dt_sel(end_date_idx, end_time_idx)

        # 8. INSERT INTO Iceberg trips table
        insert_sql = f"""
        INSERT INTO trips
        SELECT
            {gender_field} as user_gender,
            {age_field} as user_age,
            {bike_field} as bike_id,
            {start_st_field} as start_station_id,
            {end_st_field} as end_station_id,
            {start_ts_field} as start_timestamp,
            {end_ts_field} as end_timestamp
        FROM {staging_tbl}
        WHERE 
            {start_st_field} IS NOT NULL AND 
            {end_st_field} IS NOT NULL AND
            {start_ts_field} IS NOT NULL AND
            {end_ts_field} IS NOT NULL
        """
        run_athena_query(athena_client, db_name, workgroup, bucket, insert_sql, f"Insert staging {target_ym} to trips")

        logger.info("Successfully loaded month %s to Iceberg trips table.", target_ym)

        # Trigger historical status reconstruction asynchronously
        process_fn = os.environ.get("PROCESS_HISTORICAL_STATUS_FUNCTION")
        if process_fn:
            try:
                import json
                lambda_client = session.client("lambda")
                payload = {
                    "year": year,
                    "month": month
                }
                logger.info("Invoking %s for %s-%s...", process_fn, year, month)
                lambda_client.invoke(
                    FunctionName=process_fn,
                    InvocationType="Event",
                    Payload=json.dumps(payload).encode("utf-8")
                )
                logger.info("Successfully triggered historical status reconstruction.")
            except Exception as e:
                logger.error("Failed to trigger historical status reconstruction: %s", e)
        else:
            logger.warning("PROCESS_HISTORICAL_STATUS_FUNCTION environment variable not set.")

    finally:
        # 9. Cleanup
        logger.info("Cleaning up staging tables and raw CSV...")
        try:
            run_athena_query(athena_client, db_name, workgroup, bucket, f"DROP TABLE IF EXISTS {staging_tbl}", "Cleanup drop staging table")
            s3_client.delete_object(Bucket=bucket, Key=raw_s3_key)
        except Exception as e:
            logger.warning("Cleanup warning: %s", e)

    return {
        "statusCode": 200,
        "body": f"Month {target_ym} successfully ingested."
    }

