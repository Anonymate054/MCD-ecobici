"""
ingest_gbfs.py — Ecobici GBFS Station Status & Info Ingestor
=============================================================
Triggered every 5 minutes by EventBridge.

Responsibilities:
  1. Fetch station_status.json from the GBFS feed and push records to Firehose.
  2. Once daily, fetch station_information.json and write it to S3 as Parquet
     (tracked via an SSM parameter to avoid redundant daily calls).

Env vars (injected by Terraform):
  GBFS_SECRET_NAME     - Secrets Manager secret name for GBFS API credentials
  FIREHOSE_STREAM_NAME - Kinesis Data Firehose delivery stream name
  S3_BUCKET            - S3 data lake bucket name
  SSM_REFRESH_PARAM    - SSM parameter name tracking last station_info refresh date
  GLUE_DATABASE        - Glue catalog database name
  ATHENA_WORKGROUP     - Athena workgroup name
"""

import json
import logging
import os
from datetime import datetime, timezone, date
from typing import Any

import boto3
import urllib.request
import urllib.error

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# AWS clients — instantiated outside handler for connection reuse
# ---------------------------------------------------------------------------
secrets_client   = boto3.client("secretsmanager")
firehose_client  = boto3.client("firehose")
ssm_client       = boto3.client("ssm")
s3_client        = boto3.client("s3")

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
GBFS_SECRET_NAME     = os.environ["GBFS_SECRET_NAME"]
FIREHOSE_STREAM_NAME = os.environ["FIREHOSE_STREAM_NAME"]
S3_BUCKET            = os.environ["S3_BUCKET"]
SSM_REFRESH_PARAM    = os.environ["SSM_REFRESH_PARAM"]

# GBFS feed endpoint suffixes
STATUS_PATH = "/en/station_status.json"
INFO_PATH   = "/en/station_information.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_secret() -> dict:
    """Retrieve GBFS API credentials from Secrets Manager."""
    response = secrets_client.get_secret_value(SecretId=GBFS_SECRET_NAME)
    return json.loads(response["SecretString"])


def _http_get(url: str, api_key: str | None = None) -> dict:
    """Simple HTTPS GET returning parsed JSON. Raises on non-200."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} from {url}")
        raw = resp.read()
        # Try UTF-8 first; fall back to latin-1 (used by some Mexican gov APIs)
        for enc in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                return json.loads(raw.decode(enc))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        raise RuntimeError(f"Could not decode response from {url}")


def _build_status_records(stations: list[dict], ingest_ts: str) -> list[dict]:
    """Normalise raw GBFS station_status data into the target schema."""
    records = []
    for s in stations:
        records.append({
            "timestamp":       ingest_ts,
            "station_id":      str(s.get("station_id", "")),
            "bikes_available": int(s.get("num_bikes_available", 0)),
            "docks_available": int(s.get("num_docks_available", 0)),
            "is_renting":      bool(s.get("is_renting", False)),
            "is_returning":    bool(s.get("is_returning", False)),
            "_ingest_at":      ingest_ts,
        })
    return records


def _push_to_firehose(records: list[dict]) -> None:
    """Send records to Kinesis Firehose as newline-delimited JSON batches (max 500/batch)."""
    batch_size = 500
    total_sent = 0

    for i in range(0, len(records), batch_size):
        batch = records[i : i + batch_size]
        firehose_records = [
            {"Data": (json.dumps(r) + "\n").encode("utf-8")}
            for r in batch
        ]
        response = firehose_client.put_record_batch(
            DeliveryStreamName=FIREHOSE_STREAM_NAME,
            Records=firehose_records,
        )
        failed = response.get("FailedPutCount", 0)
        if failed > 0:
            logger.warning("Firehose: %d records failed in batch starting at %d", failed, i)
        total_sent += len(batch) - failed

    logger.info("Firehose: pushed %d/%d records to %s", total_sent, len(records), FIREHOSE_STREAM_NAME)


def _should_refresh_station_info() -> bool:
    """Return True if station_info has not been refreshed today (UTC)."""
    today_str = date.today().isoformat()
    response  = ssm_client.get_parameter(Name=SSM_REFRESH_PARAM)
    last_date = response["Parameter"]["Value"]
    return last_date != today_str


def _mark_station_info_refreshed() -> None:
    """Update the SSM parameter with today's date."""
    ssm_client.put_parameter(
        Name=SSM_REFRESH_PARAM,
        Value=date.today().isoformat(),
        Overwrite=True,
    )


def _write_station_info_to_s3(stations: list[dict], ingest_ts: str) -> None:
    """
    Serialize station info to newline-delimited JSON and write to S3.
    A Glue Crawler or Iceberg MERGE can pick this up for SCD Type-1 updates.
    """
    now = datetime.now(timezone.utc)
    key = (
        f"raw/station_info/"
        f"year={now.year}/month={now.month:02d}/day={now.day:02d}/"
        f"station_info_{now.strftime('%H%M%S')}.json"
    )
    records = []
    for s in stations:
        records.append({
            "station_id":  str(s.get("station_id", "")),
            "name":        s.get("name", ""),
            "lat":         float(s.get("lat", 0.0)),
            "lon":         float(s.get("lon", 0.0)),
            "capacity":    int(s.get("capacity", 0)),
            "_updated_at": ingest_ts,
        })

    body = "\n".join(json.dumps(r) for r in records)
    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="application/json",
    )
    logger.info("Station info written to s3://%s/%s (%d stations)", S3_BUCKET, key, len(records))


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handler(event: Any, context: Any) -> dict:
    """Lambda entry point."""
    ingest_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        secret = _get_secret()
        base_url = secret["url"].rstrip("/")
        api_key  = secret.get("api_key")

        # --- 1. Station Status (every invocation) ---
        logger.info("Fetching station status from %s%s", base_url, STATUS_PATH)
        status_data = _http_get(f"{base_url}{STATUS_PATH}", api_key)
        stations_status = status_data["data"]["stations"]
        logger.info("Received %d station status records", len(stations_status))

        records = _build_status_records(stations_status, ingest_ts)
        _push_to_firehose(records)

        # --- 2. Station Info (once daily) ---
        if _should_refresh_station_info():
            logger.info("Fetching station info (daily refresh)...")
            info_data = _http_get(f"{base_url}{INFO_PATH}", api_key)
            stations_info = info_data["data"]["stations"]
            _write_station_info_to_s3(stations_info, ingest_ts)
            _mark_station_info_refreshed()
            logger.info("Station info refresh complete (%d stations)", len(stations_info))
        else:
            logger.info("Station info already refreshed today — skipping.")

        return {
            "statusCode":    200,
            "stations_pushed": len(records),
            "ingest_ts":     ingest_ts,
        }

    except urllib.error.URLError as exc:
        logger.error("Network error fetching GBFS feed: %s", exc)
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Unhandled error in ingest_gbfs: %s", exc, exc_info=True)
        raise
