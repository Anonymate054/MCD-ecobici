"""
ingest_weather.py — CDMX Weather Observations Ingestor
=======================================================
Triggered every 1 hour by EventBridge.

Data source: Open-Meteo (https://open-meteo.com)
  - 100% free, no API key, no authentication required.
  - Provides current weather for any lat/lon in the world.
  - Queried per Ecobici station coordinate (fetched from S3 station_info).

Strategy:
  1. Load today's station_information from S3 (written daily by ingest_gbfs).
  2. Batch stations into groups of 100 (Open-Meteo multi-location API limit).
  3. Fetch temperature, precipitation, humidity, wind_speed for each station.
  4. Apply micro-cleaning (temp bounds, forward-fill up to 30 min per station).
  5. Push cleaned records to Kinesis Firehose.

Env vars (injected by Terraform):
  S3_BUCKET            - Data lake bucket name (reads station_info from here)
  FIREHOSE_STREAM_NAME - Kinesis Firehose delivery stream name
"""

import json
import logging
import os
import urllib.error
import urllib.request
import time
from datetime import datetime, timezone
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------
firehose_client = boto3.client("firehose")
s3_client       = boto3.client("s3")

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
S3_BUCKET            = os.environ["S3_BUCKET"]
FIREHOSE_STREAM_NAME = os.environ["FIREHOSE_STREAM_NAME"]

# Micro-cleaning constants
TEMP_MIN_C             = -10.0
TEMP_MAX_C             = 50.0
FORWARD_FILL_MAX_SLOTS = 1   # 1 × 1-hour slot = 1 hour max forward-fill

# Open-Meteo endpoint (no auth, multi-location, free tier: 10k req/day)
OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lats}&longitude={lons}"
    "&hourly=temperature_2m,precipitation"
    "&timezone=UTC"
    "&forecast_days=1"
)

# Max stations per Open-Meteo batch request
OPEN_METEO_BATCH_SIZE = 100


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _http_get(url: str) -> dict | list:
    """Unauthenticated HTTPS GET returning parsed JSON."""
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=20) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} from {url}")
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Station info loader — reads today's NDJSON from S3
# ---------------------------------------------------------------------------

def _load_station_coords() -> list[dict]:
    """
    Load station coordinates from the most recent station_info file in S3.
    Returns a list of dicts with keys: station_id, lat, lon.
    Falls back to a minimal hard-coded set of CDMX representative stations
    if no station_info file exists yet.
    """
    now    = datetime.now(timezone.utc)
    prefix = f"raw/station_info/year={now.year}/month={now.month:02d}/day={now.day:02d}/"

    resp  = s3_client.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
    files = sorted(
        [obj["Key"] for obj in resp.get("Contents", [])],
        reverse=True  # most recent first
    )

    if not files:
        logger.warning("No station_info file found for today — using fallback coords")
        return [
            {"station_id": "fallback", "lat": 19.4326, "lon": -99.1332},  # Centro CDMX
        ]

    latest = files[0]
    body   = s3_client.get_object(Bucket=S3_BUCKET, Key=latest)["Body"].read()
    stations = [
        json.loads(line)
        for line in body.decode("utf-8").strip().splitlines()
        if line.strip()
    ]
    logger.info("Loaded %d station coordinates from s3://%s/%s", len(stations), S3_BUCKET, latest)
    return stations


# ---------------------------------------------------------------------------
# Open-Meteo fetcher — batched multi-location
# ---------------------------------------------------------------------------

def _fetch_open_meteo(stations: list[dict], ingest_ts: str) -> list[dict]:
    """
    Batch-fetch hourly weather from Open-Meteo for all Ecobici stations.
    Returns a flat list of raw observation dicts matching the internal schema.
    """
    observations = []
    success_count = 0
    last_error = None

    # Round ingest_ts (UTC, e.g. "2026-06-07T03:07:12Z") to current hour
    # e.g. "2026-06-07T03:00"
    dt_utc = datetime.strptime(ingest_ts, "%Y-%m-%dT%H:%M:%SZ")
    target_time_str = dt_utc.strftime("%Y-%m-%dT%H:00")

    for i in range(0, len(stations), OPEN_METEO_BATCH_SIZE):
        # Prevent bursting requests and trigger rate limiters (pacing delay)
        if i > 0:
            time.sleep(1.0)

        batch = stations[i : i + OPEN_METEO_BATCH_SIZE]
        lats  = ",".join(str(s["lat"])        for s in batch)
        lons  = ",".join(str(s["lon"])        for s in batch)
        url   = OPEN_METEO_URL.format(lats=lats, lons=lons)

        data = None
        max_retries = 5
        backoff = 2
        for attempt in range(max_retries):
            try:
                data = _http_get(url)
                break
            except Exception as exc:
                last_error = exc
                if attempt == max_retries - 1:
                    logger.error("Open-Meteo batch %d failed after %d attempts: %s", 
                                 i // OPEN_METEO_BATCH_SIZE, max_retries, exc)
                else:
                    sleep_time = backoff ** attempt
                    logger.warning("Open-Meteo batch %d failed (attempt %d/%d): %s. Retrying in %d seconds...", 
                                   i // OPEN_METEO_BATCH_SIZE, attempt + 1, max_retries, exc, sleep_time)
                    time.sleep(sleep_time)

        if not data:
            logger.warning("Skipping batch starting at index %d due to API failures", i)
            continue

        success_count += 1

        if isinstance(data, dict):
            data = [data]

        for j, item in enumerate(data):
            hourly = item.get("hourly", {})
            times = hourly.get("time", [])

            try:
                idx = times.index(target_time_str)
            except ValueError:
                idx = -1

            if idx != -1:
                temp_c = hourly.get("temperature_2m", [])[idx]
                precip = hourly.get("precipitation", [])[idx]
            else:
                temp_c = None
                precip = 0.0

            observations.append({
                "timestamp":  ingest_ts,
                "station_id": str(batch[j]["station_id"]),
                "temp_c":     temp_c,
                "precip_mm":  precip,
                "_is_filled": False,
            })

    if len(stations) > 0 and success_count == 0 and last_error:
        raise last_error

    logger.info("Open-Meteo: fetched %d observations (%d batches)", len(observations),
                (len(stations) + OPEN_METEO_BATCH_SIZE - 1) // OPEN_METEO_BATCH_SIZE)
    return observations



# ---------------------------------------------------------------------------
# Micro-cleaning (unchanged logic — now applied to Open-Meteo data)
# ---------------------------------------------------------------------------

def _validate_temp(temp_c: Any) -> float | None:
    """Return None if temperature is out of physical bounds."""
    if temp_c is None:
        return None
    try:
        val = float(temp_c)
    except (TypeError, ValueError):
        return None
    return val if TEMP_MIN_C <= val <= TEMP_MAX_C else None


def _micro_clean(raw_observations: list[dict]) -> list[dict]:
    """
    Apply in-memory micro-cleaning per station:
      1. Validate temperature — reject out-of-bounds values.
      2. Forward-fill missing temp/precip up to FORWARD_FILL_MAX_SLOTS.
    Returns cleaned list with `_is_filled` flag set.
    """
    by_station: dict[str, list[dict]] = {}
    for obs in raw_observations:
        sid = str(obs.get("station_id", "unknown"))
        by_station.setdefault(sid, []).append(obs)

    cleaned = []
    for sid, observations in by_station.items():
        observations.sort(key=lambda o: o.get("timestamp", ""))
        last_valid: dict | None = None
        fill_counter: int       = 0

        for obs in observations:
            raw_temp   = obs.get("temp_c")
            raw_precip = obs.get("precip_mm")

            valid_temp   = _validate_temp(raw_temp)
            valid_precip = float(raw_precip) if raw_precip is not None else None
            is_filled    = False

            if valid_temp is None:
                if last_valid is not None and fill_counter < FORWARD_FILL_MAX_SLOTS:
                    valid_temp   = last_valid["temp_c"]
                    valid_precip = last_valid["precip_mm"]
                    fill_counter += 1
                    is_filled    = True
                    logger.debug("Forward-filled station %s (slot %d/%d): temp=%.1f",
                                 sid, fill_counter, FORWARD_FILL_MAX_SLOTS, valid_temp)
                else:
                    fill_counter = 0
                    last_valid   = None
                    logger.warning("Dropped invalid obs for station %s (temp=%s)", sid, raw_temp)
                    continue
            else:
                fill_counter = 0
                last_valid   = {"temp_c": valid_temp, "precip_mm": valid_precip}

            cleaned.append({
                "timestamp":  obs.get("timestamp",
                                      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
                "station_id": sid,
                "temp_c":     valid_temp,
                "precip_mm":  valid_precip if valid_precip is not None else 0.0,
                "_is_filled": is_filled,
            })

    return cleaned


# ---------------------------------------------------------------------------
# Firehose batcher
# ---------------------------------------------------------------------------

def _push_to_firehose(records: list[dict]) -> None:
    """Send cleaned records to Firehose in batches of 500."""
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
    logger.info("Firehose: pushed %d/%d weather records to %s",
                total_sent, len(records), FIREHOSE_STREAM_NAME)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handler(event: Any, context: Any) -> dict:
    """Lambda entry point."""
    ingest_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        # 1. Load station coordinates (source of truth for spatial anchoring)
        stations = _load_station_coords()

        # 2. Fetch weather from Open-Meteo (free, no auth)
        logger.info("Fetching weather for %d Ecobici stations from Open-Meteo", len(stations))
        raw_observations = _fetch_open_meteo(stations, ingest_ts)
        logger.info("Received %d raw observations", len(raw_observations))

        if not raw_observations:
            logger.warning("No observations received from Open-Meteo.")
            return {"statusCode": 200, "records_pushed": 0}

        # 3. Micro-clean
        cleaned = _micro_clean(raw_observations)
        dropped = len(raw_observations) - len(cleaned)
        logger.info("Micro-cleaning: %d kept, %d dropped", len(cleaned), dropped)

        # 4. Push to Firehose
        _push_to_firehose(cleaned)

        return {
            "statusCode":      200,
            "raw_received":    len(raw_observations),
            "records_pushed":  len(cleaned),
            "records_dropped": dropped,
            "ingest_ts":       ingest_ts,
        }

    except urllib.error.URLError as exc:
        logger.error("Network error fetching weather data: %s", exc)
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Unhandled error in ingest_weather: %s", exc, exc_info=True)
        raise
