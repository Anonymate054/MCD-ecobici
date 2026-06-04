import boto3
import time
import datetime
import csv
import io

session = boto3.Session(profile_name='ecobici-de-01')
athena = session.client('athena', region_name='us-east-1')
s3 = session.client('s3')
glue = session.client('glue')

BUCKET    = 'ecobici-datalake-195202617652'
WORKGROUP = 'ecobici-workgroup'
RESULTS   = f's3://{BUCKET}/athena-results/'

def q(sql):
    print(f"Running: {sql[:120]}...")
    r = athena.start_query_execution(
        QueryString=sql, QueryExecutionContext={'Database': 'ecobici_lake'},
        ResultConfiguration={'OutputLocation': RESULTS}, WorkGroup=WORKGROUP
    )
    qid = r['QueryExecutionId']
    for _ in range(60):
        st = athena.get_query_execution(QueryExecutionId=qid)['QueryExecution']['Status']
        if st['State'] == 'SUCCEEDED': break
        if st['State'] in ('FAILED','CANCELLED'):
            raise RuntimeError(f"Query failed: {st.get('StateChangeReason')}")
        time.sleep(2)
    
    # Check if SELECT or DML
    try:
        rows = athena.get_query_results(QueryExecutionId=qid)['ResultSet']['Rows']
    except Exception:
        # Non-SELECT statement
        return []
        
    if not rows: return []
    h = [c.get('VarCharValue','') for c in rows[0]['Data']]
    return [{h[i]: col.get('VarCharValue','') for i,col in enumerate(r['Data'])} for r in rows[1:]]

def parse_ts(ts_str):
    # Parse timestamps like "2026-06-04 10:56:42.000000" or "2026-06-04 10:56:42" or ISO
    if 'T' in ts_str:
        ts_str = ts_str.replace('T', ' ').replace('Z', '')
    if '.' in ts_str:
        ts_str = ts_str.split('.')[0]
    return datetime.datetime.strptime(ts_str.strip(), "%Y-%m-%d %H:%M:%S")

def main():
    # 1. Fetch the boundary weather records
    print("Fetching boundaries...")
    sql_boundaries = """
    SELECT station_id, timestamp, temp_c, precip_mm 
    FROM weather_observations 
    WHERE timestamp IN (timestamp '2026-06-04 10:56:42.000', timestamp '2026-06-04 14:26:42.000')
    """
    records = q(sql_boundaries)
    print(f"Found {len(records)} boundary records.")
    
    # Group by station_id
    by_station = {}
    for r in records:
        sid = r['station_id']
        ts = r['timestamp']
        temp = float(r['temp_c']) if r['temp_c'] else None
        prec = float(r['precip_mm']) if r['precip_mm'] else 0.0
        by_station.setdefault(sid, {})[ts] = (temp, prec)
        
    # Interpolation parameters
    t0_dt = parse_ts("2026-06-04 10:56:42")
    t1_dt = parse_ts("2026-06-04 14:26:42")
    t0 = t0_dt.timestamp()
    t1 = t1_dt.timestamp()
    
    # Target hours (UTC)
    target_hours = [
        "2026-06-04 11:00:00",
        "2026-06-04 12:00:00",
        "2026-06-04 13:00:00",
        "2026-06-04 14:00:00"
    ]
    
    interpolated_rows = []
    
    for sid, data in by_station.items():
        # Find the keys in data that correspond to t0 and t1
        t0_data = None
        t1_data = None
        for k, v in data.items():
            k_dt = parse_ts(k)
            if k_dt == t0_dt:
                t0_data = v
            elif k_dt == t1_dt:
                t1_data = v
                
        if t0_data is not None and t1_data is not None:
            temp0, prec0 = t0_data
            temp1, prec1 = t1_data
            
            if temp0 is None or temp1 is None:
                continue
                
            for target_str in target_hours:
                target_dt = parse_ts(target_str)
                target_t = target_dt.timestamp()
                
                # Interpolate temp
                fraction = (target_t - t0) / (t1 - t0)
                temp_interp = round(temp0 + (temp1 - temp0) * fraction, 2)
                
                # Interpolate precip
                precip_interp = round(prec0 + (prec1 - prec0) * fraction, 2)
                
                # ISO Format for Athena's from_iso8601_timestamp
                iso_timestamp = target_str.replace(" ", "T") + "Z"
                
                interpolated_rows.append({
                    'timestamp': iso_timestamp,
                    'station_id': sid,
                    'temp_c': str(temp_interp),
                    'precip_mm': str(max(0.0, precip_interp)),
                    '_is_filled': 'true'
                })
                
    print(f"Generated {len(interpolated_rows)} interpolated rows.")
    
    if not interpolated_rows:
        print("No rows generated. Exiting.")
        return
        
    # Write to CSV in S3
    csv_buffer = io.StringIO()
    writer = csv.DictWriter(csv_buffer, fieldnames=['timestamp', 'station_id', 'temp_c', 'precip_mm', '_is_filled'])
    writer.writeheader()
    for row in interpolated_rows:
        writer.writerow(row)
        
    s3_key = 'tmp/interpolated_weather.csv'
    
    # Wait, Glue table location must be a directory prefix, not a file key!
    # Let's copy it to tmp/interpolated_weather_dir/interpolated_weather.csv
    s3_dir_key = 'tmp/interpolated_weather_dir/interpolated_weather.csv'
    print(f"Uploading CSV to S3: s3://{BUCKET}/{s3_dir_key}")
    s3.put_object(
        Bucket=BUCKET,
        Key=s3_dir_key,
        Body=csv_buffer.getvalue().encode('utf-8')
    )
    
    # Create Staging Table in Glue Catalog
    staging_tbl = 'staging_interpolated_weather'
    print(f"Creating staging table {staging_tbl}...")
    try:
        glue.delete_table(DatabaseName='ecobici_lake', Name=staging_tbl)
    except Exception:
        pass
        
    # Update table location
    glue.create_table(
        DatabaseName='ecobici_lake',
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
                    {'Name': 'temp_c', 'Type': 'double'},
                    {'Name': 'precip_mm', 'Type': 'double'},
                    {'Name': 'is_filled', 'Type': 'boolean'}
                ],
                'Location': f's3://{BUCKET}/tmp/interpolated_weather_dir/',
                'InputFormat': 'org.apache.hadoop.mapred.TextInputFormat',
                'OutputFormat': 'org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat',
                'SerdeInfo': {
                    'SerializationLibrary': 'org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe',
                    'Parameters': {
                        'field.delim': ',',
                        'serialization.format': ','
                    }
                }
            }
        }
    )
    
    # 2. Insert into weather_observations
    print("Inserting into weather_observations...")
    sql_insert = f"""
    INSERT INTO weather_observations
    SELECT 
        CAST(from_iso8601_timestamp(timestamp) AS TIMESTAMP) AS timestamp,
        station_id,
        temp_c,
        precip_mm,
        is_filled AS _is_filled
    FROM {staging_tbl}
    """
    q(sql_insert)
    print("Insertion complete.")
    
    # Clean up staging table and file
    print("Cleaning up staging...")
    try:
        glue.delete_table(DatabaseName='ecobici_lake', Name=staging_tbl)
        s3.delete_object(Bucket=BUCKET, Key=s3_dir_key)
    except Exception as e:
        print("Cleanup error:", e)
        
    # 3. Update hourly_station_status
    # First delete existing records for those hours
    print("Deleting old rollup records from hourly_station_status...")
    sql_delete = """
    DELETE FROM hourly_station_status
    WHERE hour IN (
        timestamp '2026-06-04 11:00:00',
        timestamp '2026-06-04 12:00:00',
        timestamp '2026-06-04 13:00:00',
        timestamp '2026-06-04 14:00:00'
    )
    """
    q(sql_delete)
    
    # Re-run rollup for those hours by manually triggering the rollup SQL logic
    print("Re-running rollup to calculate correct weather...")
    # Rollup SQL with explicit hour filter
    sql_rollup = """
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
        WHERE "timestamp" >= timestamp '2026-06-04 11:00:00'
          AND "timestamp" < timestamp '2026-06-04 15:00:00'
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
        WHERE "timestamp" >= timestamp '2026-06-04 11:00:00'
          AND "timestamp" < timestamp '2026-06-04 15:00:00'
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
    WHERE f.hour IN (
        timestamp '2026-06-04 11:00:00',
        timestamp '2026-06-04 12:00:00',
        timestamp '2026-06-04 13:00:00',
        timestamp '2026-06-04 14:00:00'
    )
    ORDER BY f.hour, f.station_id
    """
    q(sql_rollup)
    print("Rollup calculation complete! Data has been fully updated.")

if __name__ == '__main__':
    main()
