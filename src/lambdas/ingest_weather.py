"""
ingest_weather.py — Institutional Weather Observations Ingestor
===============================================================
Triggered every 10 minutes by EventBridge.

Fetches observations from an institutional weather API
(SMN / REDMET / OH-UNAM — configured via Secrets Manager).

In-memory micro-cleaning:
  - Rejects temperature readings outside [-10°C, 50°C].
  - Forward-fills missing readings up to 30 minutes (3 consecutive slots).

Env vars (injected by Terraform):
  WEATHER_SECRET_NAME  - Secrets Manager secret name for weather API credentials
  FIREHOSE_STREAM_NAME - Kinesis Data Firehose delivery stream name
"""

import json
import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import Any

import boto3
import urllib.request
import urllib.error

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------
secrets_client  = boto3.client("secretsmanager")
firehose_client = boto3.client("firehose")

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
WEATHER_SECRET_NAME  = os.environ["WEATHER_SECRET_NAME"]
FIREHOSE_STREAM_NAME = os.environ["FIREHOSE_STREAM_NAME"]

# Micro-cleaning constants
TEMP_MIN_C            = -10.0
TEMP_MAX_C            = 50.0
FORWARD_FILL_MAX_SLOTS = 3   # 3 × 10-min slots = 30 minutes max forward-fill


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_secret() -> dict:
    """Retrieve weather API credentials from Secrets Manager."""
    response = secrets_client.get_secret_value(SecretId=WEATHER_SECRET_NAME)
    return json.loads(response["SecretString"])


def _http_get(url: str, api_key: str | None = None) -> dict:
    """Simple HTTPS GET returning parsed JSON."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} from {url}")
        raw = resp.read()
        for enc in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                return json.loads(raw.decode(enc))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        raise RuntimeError(f"Could not decode response from {url}")


def _validate_temp(temp_c: float | None) -> float | None:
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
    Apply in-memory micro-cleaning to a list of raw weather observations.

    Steps:
      1. Validate temperatures — reject out-of-bounds values.
      2. Forward-fill missing temp/precip up to FORWARD_FILL_MAX_SLOTS per station.

    Returns a cleaned list with `_is_filled` flag.
    """
    # Group by station_id to forward-fill independently per station
    by_station: dict[str, list[dict]] = {}
    for obs in raw_observations:
        sid = str(obs.get("station_id", "unknown"))
        by_station.setdefault(sid, []).append(obs)

    cleaned = []
    for sid, observations in by_station.items():
        # Sort by timestamp ascending for correct forward-fill order
        observations.sort(key=lambda o: o.get("timestamp", ""))

        # Rolling buffer: store (temp_c, precip_mm, slots_filled)
        last_valid: dict | None = None
        fill_counter: int = 0

        for obs in observations:
            raw_temp   = obs.get("temp_c")
            raw_precip = obs.get("precip_mm")

            valid_temp   = _validate_temp(raw_temp)
            valid_precip = float(raw_precip) if raw_precip is not None else None

            is_filled = False

            if valid_temp is None:
                # Try forward-fill
                if last_valid is not None and fill_counter < FORWARD_FILL_MAX_SLOTS:
                    valid_temp   = last_valid["temp_c"]
                    valid_precip = last_valid["precip_mm"]
                    fill_counter += 1
                    is_filled = True
                    logger.debug(
                        "Forward-filled station %s (slot %d/%d): temp=%.1f",
                        sid, fill_counter, FORWARD_FILL_MAX_SLOTS, valid_temp,
                    )
                else:
                    # Cannot fill — drop the record
                    fill_counter = 0
                    last_valid   = None
                    logger.warning(
                        "Dropped invalid observation for station %s (temp=%s)", sid, raw_temp
                    )
                    continue
            else:
                fill_counter = 0
                last_valid = {"temp_c": valid_temp, "precip_mm": valid_precip}

            cleaned.append({
                "timestamp":  obs.get("timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
                "station_id": sid,
                "temp_c":     valid_temp,
                "precip_mm":  valid_precip if valid_precip is not None else 0.0,
                "_is_filled": is_filled,
            })

    return cleaned


def _push_to_firehose(records: list[dict]) -> None:
    """Send cleaned records to Firehose in batches of 500."""
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

    logger.info(
        "Firehose: pushed %d/%d weather records to %s",
        total_sent, len(records), FIREHOSE_STREAM_NAME,
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handler(event: Any, context: Any) -> dict:
    """Lambda entry point."""
    try:
        secret   = _get_secret()
        base_url = secret["url"].rstrip("/")
        api_key  = secret.get("api_key")

        logger.info("Fetching weather observations from %s", base_url)
        raw_data = _http_get(base_url, api_key)

        # The institutional API response is expected under `observations` or `data`
        raw_observations = (
            raw_data.get("observations")
            or raw_data.get("data", {}).get("observations")
            or []
        )
        logger.info("Received %d raw observations", len(raw_observations))

        if not raw_observations:
            logger.warning("Empty observations list — check API endpoint or key.")
            return {"statusCode": 200, "records_pushed": 0}

        cleaned = _micro_clean(raw_observations)
        dropped = len(raw_observations) - len(cleaned)
        logger.info(
            "Micro-cleaning: %d kept, %d dropped (%.1f%% drop rate)",
            len(cleaned), dropped,
            (dropped / len(raw_observations) * 100) if raw_observations else 0,
        )

        _push_to_firehose(cleaned)

        return {
            "statusCode":    200,
            "raw_received":  len(raw_observations),
            "records_pushed": len(cleaned),
            "records_dropped": dropped,
        }

    except urllib.error.URLError as exc:
        logger.error("Network error fetching weather data: %s", exc)
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Unhandled error in ingest_weather: %s", exc, exc_info=True)
        raise
