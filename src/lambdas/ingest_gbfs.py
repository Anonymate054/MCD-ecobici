"""
ingest_gbfs.py — Ecobici GBFS Station Status & Info Ingestor
=============================================================
Triggered every 5 minutes by EventBridge.

The Lyft/Ecobici CDMX GBFS feed is 100% public — no API key or auth required.
The discovery URL (gbfs.json) is read first to auto-resolve the feed URLs,
making the Lambda resilient to future endpoint URL changes.

Responsibilities:
  1. Auto-discover feed URLs from the GBFS discovery endpoint.
  2. Fetch station_status.json and push records to Kinesis Firehose.
  3. Once daily, fetch station_information.json and write to S3
     (gated by an SSM parameter to avoid redundant calls).

Env vars (injected by Terraform):
  GBFS_DISCOVERY_URL   - Public GBFS discovery URL (no auth)
  FIREHOSE_STREAM_NAME - Kinesis Data Firehose delivery stream name
  S3_BUCKET            - S3 data lake bucket name
  SSM_REFRESH_PARAM    - SSM parameter tracking last station_info refresh date
  GLUE_DATABASE        - Glue catalog database name
  ATHENA_WORKGROUP     - Athena workgroup name
"""

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# AWS clients (outside handler for connection reuse)
# ---------------------------------------------------------------------------
firehose_client = boto3.client("firehose")
ssm_client      = boto3.client("ssm")
s3_client       = boto3.client("s3")

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
GBFS_DISCOVERY_URL   = os.environ["GBFS_DISCOVERY_URL"]
FIREHOSE_STREAM_NAME = os.environ["FIREHOSE_STREAM_NAME"]
S3_BUCKET            = os.environ["S3_BUCKET"]
SSM_REFRESH_PARAM    = os.environ["SSM_REFRESH_PARAM"]


# ---------------------------------------------------------------------------
# HTTP helper — unauthenticated, encoding-resilient
# ---------------------------------------------------------------------------

def _http_get(url: str) -> dict:
    """Simple HTTPS GET returning parsed JSON. No authentication required."""
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} from {url}")
        raw = resp.read()
        for enc in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                return json.loads(raw.decode(enc))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        raise RuntimeError(f"Could not decode response from {url}")


# ---------------------------------------------------------------------------
# GBFS auto-discovery
# ---------------------------------------------------------------------------

def _discover_feed_urls() -> dict[str, str]:
    """
    Fetch the GBFS discovery document and return a mapping of feed name → URL.
    Prefers the 'en' language feed; falls back to the first available language.

    Example result:
        {
          "station_status":      "https://.../en/station_status.json",
          "station_information": "https://.../en/station_information.json",
          ...
        }
    """
    data = _http_get(GBFS_DISCOVERY_URL)
    feeds_by_lang = data.get("data", {})

    # Prefer English feeds
    lang_feeds = feeds_by_lang.get("en") or next(iter(feeds_by_lang.values()), {})
    feeds = lang_feeds.get("feeds", [])

    url_map = {feed["name"]: feed["url"] for feed in feeds}
    logger.info("Discovered %d GBFS feeds: %s", len(url_map), list(url_map.keys()))
    return url_map


# ---------------------------------------------------------------------------
# Record builders
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Firehose batcher
# ---------------------------------------------------------------------------

def _push_to_firehose(records: list[dict]) -> None:
    """Send records to Kinesis Firehose as newline-delimited JSON (max 500/batch)."""
    total_sent = 0
    for i in range(0, len(records), 500):
        batch = records[i : i + 500]
        response = firehose_client.put_record_batch(
            DeliveryStreamName=FIREHOSE_STREAM_NAME,
            Records=[{"Data": (json.dumps(r) + "\n").encode("utf-8")} for r in batch],
        )
        failed = response.get("FailedPutCount", 0)
        if failed > 0:
            logger.warning("Firehose: %d records failed in batch at offset %d", failed, i)
        total_sent += len(batch) - failed

    logger.info("Firehose: pushed %d/%d records to %s", total_sent, len(records), FIREHOSE_STREAM_NAME)


# ---------------------------------------------------------------------------
# SSM-gated daily station_info refresh
# ---------------------------------------------------------------------------

def _should_refresh_station_info() -> bool:
    """Return True if station_info has not been refreshed today (UTC)."""
    response  = ssm_client.get_parameter(Name=SSM_REFRESH_PARAM)
    last_date = response["Parameter"]["Value"]
    return last_date != date.today().isoformat()


def _mark_station_info_refreshed() -> None:
    """Update the SSM parameter with today's date."""
    ssm_client.put_parameter(Name=SSM_REFRESH_PARAM, Value=date.today().isoformat(), Overwrite=True)


def _write_station_info_to_s3(stations: list[dict], ingest_ts: str) -> None:
    """Write station_information records as newline-delimited JSON to S3."""
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
        # Auto-discover feed URLs from the public GBFS manifest
        feed_urls = _discover_feed_urls()

        status_url = feed_urls.get("station_status")
        info_url   = feed_urls.get("station_information")

        if not status_url:
            raise RuntimeError("station_status feed not found in GBFS discovery document")

        # ── 1. Station Status (every 5-min invocation) ──────────────────────
        logger.info("Fetching station_status from %s", status_url)
        status_data     = _http_get(status_url)
        stations_status = status_data["data"]["stations"]
        logger.info("Received %d station status records", len(stations_status))

        records = _build_status_records(stations_status, ingest_ts)
        _push_to_firehose(records)

        # ── 2. Station Info (once daily, SSM-gated) ─────────────────────────
        if info_url and _should_refresh_station_info():
            logger.info("Fetching station_information (daily refresh) from %s", info_url)
            info_data     = _http_get(info_url)
            stations_info = info_data["data"]["stations"]
            _write_station_info_to_s3(stations_info, ingest_ts)
            _mark_station_info_refreshed()
            logger.info("Station info refresh complete (%d stations)", len(stations_info))
        else:
            logger.info("Station info already refreshed today — skipping.")

        return {
            "statusCode":      200,
            "stations_pushed": len(records),
            "ingest_ts":       ingest_ts,
        }

    except urllib.error.URLError as exc:
        logger.error("Network error fetching GBFS feed: %s", exc)
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Unhandled error in ingest_gbfs: %s", exc, exc_info=True)
        raise
