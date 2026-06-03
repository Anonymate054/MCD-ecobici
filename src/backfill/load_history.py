"""
load_history.py — Historical Ecobici CSV Backfill Script
=========================================================
One-off script that downloads monthly Ecobici trip CSVs (2023-present),
transforms origin-destination trip data into per-station availability deltas
matching the hourly_station_status schema, and writes Parquet files to S3.

Usage:
    python load_history.py \\
        --start-month 2023-01 \\
        --end-month   2026-05 \\
        --s3-bucket   ecobici-datalake-<account_id> \\
        --s3-prefix   processed/hourly_backfill/

Requirements:
    pip install boto3 pandas requests tqdm pyarrow
"""

import argparse
import io
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pandas as pd
import requests
from dateutil.relativedelta import relativedelta
from tqdm import tqdm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ecobici open data CSV URL pattern
# (Update if the city changes the data portal URL)
# ---------------------------------------------------------------------------
CSV_URL_TEMPLATE = (
    "https://ecobici.cdmx.gob.mx/wp-content/uploads/"
    "{year}/{month:02d}/OD_{year}{month:02d}.csv"
)

# ---------------------------------------------------------------------------
# PII columns to drop before writing to S3
# These columns MUST be removed for privacy compliance and cost optimisation.
# ---------------------------------------------------------------------------
PII_COLUMNS = [
    "Genero_Usuario",
    "Edad_Usuario",
    "edad_usuario",
    "genero_usuario",
    "gender",
    "age",
    "user_id",
    "Usuario_Id",
    "id_usuario",
]

# Trip columns expected in the raw CSV
REQUIRED_COLS = {
    "Ciclo_Estacion_Retiro",   # origin station
    "Ciclo_Estacion_Arribo",   # destination station
    "Fecha_Retiro",            # departure date  (DD/MM/YYYY)
    "Hora_Retiro",             # departure time  (HH:MM:SS)
    "Fecha_Arribo",            # arrival date
    "Hora_Arribo",             # arrival time
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _month_range(start: str, end: str):
    """Yield (year, month) tuples from start to end inclusive (YYYY-MM format)."""
    current = datetime.strptime(start, "%Y-%m")
    end_dt  = datetime.strptime(end,   "%Y-%m")
    while current <= end_dt:
        yield current.year, current.month
        current += relativedelta(months=1)


def _download_csv(year: int, month: int) -> pd.DataFrame | None:
    """
    Download monthly CSV from the Ecobici data portal.
    Returns None if the file is not yet available (HTTP 404).
    """
    url = CSV_URL_TEMPLATE.format(year=year, month=month)
    logger.info("Downloading %s", url)

    try:
        resp = requests.get(url, timeout=60)
        if resp.status_code == 404:
            logger.warning("CSV not found for %d-%02d — skipping.", year, month)
            return None
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Failed to download %s: %s", url, exc)
        return None

    # Try common encodings used by Mexican government data portals
    for encoding in ("utf-8-sig", "latin-1", "utf-8"):
        try:
            df = pd.read_csv(io.StringIO(resp.content.decode(encoding)), low_memory=False)
            logger.info("Downloaded %d rows for %d-%02d (encoding=%s)", len(df), year, month, encoding)
            return df
        except (UnicodeDecodeError, pd.errors.ParserError):
            continue

    logger.error("Could not decode CSV for %d-%02d with any known encoding.", year, month)
    return None


def _sanitize(df: pd.DataFrame, year: int, month: int) -> pd.DataFrame:
    """
    Drop PII, coerce numerics, and validate required columns.
    Rows that fail parsing are silently dropped (logged).
    """
    original_len = len(df)

    # Normalize column names to lowercase for consistent matching
    df.columns = df.columns.str.strip()

    # Drop PII columns (case-insensitive)
    pii_found = [c for c in df.columns if c in PII_COLUMNS or c.lower() in [p.lower() for p in PII_COLUMNS]]
    if pii_found:
        df = df.drop(columns=pii_found)
        logger.info("Dropped PII columns: %s", pii_found)

    # Validate required columns exist (flexible: accept lowercase variants)
    col_map = {c.lower(): c for c in df.columns}
    for req in REQUIRED_COLS:
        if req not in df.columns and req.lower() not in col_map:
            logger.error("Missing required column '%s' in %d-%02d — skipping month.", req, year, month)
            return pd.DataFrame()

    # Coerce station IDs to numeric, drop non-parseable rows
    for col in ["Ciclo_Estacion_Retiro", "Ciclo_Estacion_Arribo"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    before_drop = len(df)
    df = df.dropna(subset=[c for c in ["Ciclo_Estacion_Retiro", "Ciclo_Estacion_Arribo"] if c in df.columns])
    dropped = before_drop - len(df)
    if dropped:
        logger.info("Dropped %d corrupted/unparseable rows for %d-%02d", dropped, year, month)

    return df


def _parse_datetime_col(df: pd.DataFrame, date_col: str, time_col: str) -> pd.Series:
    """Parse date + time columns into a UTC-aware datetime series."""
    combined = df[date_col].astype(str).str.strip() + " " + df[time_col].astype(str).str.strip()
    # Handle both DD/MM/YYYY and YYYY-MM-DD formats
    parsed = pd.to_datetime(combined, dayfirst=True, errors="coerce")
    return parsed


def _compute_hourly_deltas(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert origin-destination trip records into per-station, per-hour
    availability deltas.

    Logic:
      - Each departure from station X at hour H → bikes_available decreases by 1
        (departure_delta = -1)
      - Each arrival at station Y at hour H → bikes_available increases by 1
        (arrival_delta = +1)
      - Net delta per station per hour = arrivals - departures
      - We reconstruct avg_bikes_available as a running cumulative sum
        (starting from capacity; here we use relative deltas only).

    Output schema matches hourly_station_status (subset of fields).
    """
    df = df.copy()

    # Parse timestamps
    df["departure_ts"] = _parse_datetime_col(df, "Fecha_Retiro", "Hora_Retiro")
    df["arrival_ts"]   = _parse_datetime_col(df, "Fecha_Arribo", "Hora_Arribo")

    # Drop rows with unparseable timestamps
    before = len(df)
    df = df.dropna(subset=["departure_ts", "arrival_ts"])
    dropped = before - len(df)
    if dropped:
        logger.info("Dropped %d rows with unparseable timestamps", dropped)

    # Truncate to hour
    df["departure_hour"] = df["departure_ts"].dt.floor("h")
    df["arrival_hour"]   = df["arrival_ts"].dt.floor("h")

    # Departures: -1 per trip from origin station
    departures = (
        df.groupby(["departure_hour", "Ciclo_Estacion_Retiro"])
          .size()
          .reset_index(name="departures")
          .rename(columns={"departure_hour": "hour", "Ciclo_Estacion_Retiro": "station_id"})
    )

    # Arrivals: +1 per trip to destination station
    arrivals = (
        df.groupby(["arrival_hour", "Ciclo_Estacion_Arribo"])
          .size()
          .reset_index(name="arrivals")
          .rename(columns={"arrival_hour": "hour", "Ciclo_Estacion_Arribo": "station_id"})
    )

    # Merge and compute net delta
    merged = pd.merge(departures, arrivals, on=["hour", "station_id"], how="outer").fillna(0)
    merged["station_id"]         = merged["station_id"].astype(int).astype(str)
    merged["net_delta"]          = merged["arrivals"] - merged["departures"]
    merged["avg_bikes_available"] = merged["net_delta"]  # relative delta (not absolute)
    merged["avg_docks_available"] = 0.0                  # unknown from trip data
    merged["total_renting_minutes"]   = 0
    merged["total_returning_minutes"] = 0
    merged["is_heuristically_broken"] = False
    merged["temp_c"]    = None
    merged["precip_mm"] = 0.0

    return merged[
        ["hour", "station_id", "avg_bikes_available", "avg_docks_available",
         "total_renting_minutes", "total_returning_minutes",
         "is_heuristically_broken", "temp_c", "precip_mm"]
    ]


def _write_parquet_to_s3(df: pd.DataFrame, s3_client, bucket: str, prefix: str,
                          year: int, month: int) -> None:
    """Write DataFrame as Parquet to S3 under the given prefix."""
    key = f"{prefix.rstrip('/')}/year={year}/month={month:02d}/backfill_{year}{month:02d}.parquet"

    buffer = io.BytesIO()
    df.to_parquet(buffer, index=False, engine="pyarrow", compression="snappy")
    buffer.seek(0)

    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=buffer.getvalue(),
        ContentType="application/octet-stream",
    )
    logger.info(
        "Wrote %d rows to s3://%s/%s (%.1f KB)",
        len(df), bucket, key, buffer.getbuffer().nbytes / 1024,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Ecobici historical CSV backfill to S3.")
    parser.add_argument("--start-month", required=True, help="Start month (YYYY-MM)")
    parser.add_argument("--end-month",   required=True, help="End month (YYYY-MM)")
    parser.add_argument("--s3-bucket",   required=True, help="S3 bucket name")
    parser.add_argument("--s3-prefix",   default="processed/hourly_backfill/", help="S3 key prefix")
    parser.add_argument("--aws-profile",  default=None, help="AWS CLI profile name")
    args = parser.parse_args()

    session   = boto3.Session(profile_name=args.aws_profile)
    s3_client = session.client("s3")

    months = list(_month_range(args.start_month, args.end_month))
    logger.info("Starting backfill: %d months (%s → %s)", len(months), args.start_month, args.end_month)

    success_count = 0
    skip_count    = 0
    fail_count    = 0

    for year, month in tqdm(months, desc="Backfilling months"):
        raw_df = _download_csv(year, month)
        if raw_df is None:
            skip_count += 1
            continue

        clean_df = _sanitize(raw_df, year, month)
        if clean_df.empty:
            fail_count += 1
            continue

        hourly_df = _compute_hourly_deltas(clean_df)
        if hourly_df.empty:
            logger.warning("No hourly data for %d-%02d after transformation.", year, month)
            fail_count += 1
            continue

        try:
            _write_parquet_to_s3(hourly_df, s3_client, args.s3_bucket, args.s3_prefix, year, month)
            success_count += 1
        except Exception as exc:
            logger.error("Failed to write Parquet for %d-%02d: %s", year, month, exc)
            fail_count += 1

    logger.info(
        "Backfill complete — success: %d, skipped: %d, failed: %d",
        success_count, skip_count, fail_count,
    )
    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
