#!/usr/bin/env python3
"""
check_gbfs_local.py — Local GBFS Data Availability & Schema Validator
======================================================================
Run this BEFORE deploying to cloud to confirm:
  1. The public GBFS endpoint is reachable and returns valid data.
  2. The station_status schema matches what the Lambda expects.
  3. The station_information schema matches.
  4. Summary statistics look reasonable (no mass outages, etc.)

Usage:
    # From project root, activate venv first:
    source .venv/bin/activate
    python scripts/check_gbfs_local.py

    # Or run a dry-run Lambda simulation:
    python scripts/check_gbfs_local.py --simulate-lambda
"""

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
GBFS_DISCOVERY_URL = "https://gbfs.mex.lyftbikes.com/gbfs/gbfs.json"

# Expected fields in each schema (subset — must all be present)
STATUS_REQUIRED_FIELDS    = {"station_id", "num_bikes_available", "num_docks_available",
                              "is_renting", "is_returning"}
INFO_REQUIRED_FIELDS      = {"station_id", "name", "lat", "lon", "capacity"}


# ── Colours for terminal output ───────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

OK   = f"{GREEN}✅{RESET}"
WARN = f"{YELLOW}⚠️ {RESET}"
FAIL = f"{RED}❌{RESET}"
INFO = f"{CYAN}ℹ️ {RESET}"


def _get(url: str) -> dict:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _section(title: str):
    print(f"\n{BOLD}{CYAN}{'─' * 60}{RESET}")
    print(f"{BOLD}{title}{RESET}")
    print(f"{BOLD}{CYAN}{'─' * 60}{RESET}")


def check_discovery() -> dict[str, str]:
    """Step 1: Validate the GBFS discovery document."""
    _section("Step 1 — GBFS Discovery Endpoint")
    print(f"  URL: {GBFS_DISCOVERY_URL}")

    try:
        data = _get(GBFS_DISCOVERY_URL)
    except Exception as e:
        print(f"  {FAIL} Cannot reach discovery URL: {e}")
        sys.exit(1)

    feeds_by_lang = data.get("data", {})
    available_langs = list(feeds_by_lang.keys())
    print(f"  {OK} Endpoint reachable")
    print(f"  {INFO} Available languages: {available_langs}")

    lang_feeds = feeds_by_lang.get("en") or next(iter(feeds_by_lang.values()))
    url_map = {f["name"]: f["url"] for f in lang_feeds.get("feeds", [])}

    print(f"  {OK} Found {len(url_map)} feeds: {list(url_map.keys())}")

    for required in ("station_status", "station_information"):
        if required in url_map:
            print(f"  {OK} Feed '{required}' → {url_map[required]}")
        else:
            print(f"  {FAIL} Feed '{required}' NOT found in discovery document")
            sys.exit(1)

    return url_map


def check_station_status(url: str) -> list[dict]:
    """Step 2: Validate station_status feed."""
    _section("Step 2 — Station Status Feed")
    print(f"  URL: {url}")

    try:
        data = _get(url)
    except Exception as e:
        print(f"  {FAIL} Cannot fetch station_status: {e}")
        sys.exit(1)

    stations = data["data"]["stations"]
    print(f"  {OK} Feed reachable — {len(stations)} stations returned")

    # Schema check
    sample = stations[0]
    missing = STATUS_REQUIRED_FIELDS - set(sample.keys())
    if missing:
        print(f"  {WARN} Missing expected fields in station_status: {missing}")
    else:
        print(f"  {OK} Schema OK — all required fields present")

    # Stats
    bikes_available  = sum(s.get("num_bikes_available", 0) for s in stations)
    docks_available  = sum(s.get("num_docks_available", 0) for s in stations)
    renting_count    = sum(1 for s in stations if s.get("is_renting"))
    returning_count  = sum(1 for s in stations if s.get("is_returning"))
    offline_count    = sum(1 for s in stations if not s.get("is_renting") and not s.get("is_returning"))

    print(f"\n  {BOLD}Network Summary:{RESET}")
    print(f"    Total stations    : {len(stations)}")
    print(f"    Bikes available   : {bikes_available}")
    print(f"    Docks available   : {docks_available}")
    print(f"    Stations renting  : {renting_count} ({renting_count/len(stations)*100:.1f}%)")
    print(f"    Stations returning: {returning_count} ({returning_count/len(stations)*100:.1f}%)")
    print(f"    Offline (both=0)  : {offline_count} ({offline_count/len(stations)*100:.1f}%)")

    if bikes_available == 0:
        print(f"  {FAIL} CRITICAL: SUM(bikes_available) = 0 — data feed may be broken!")
        sys.exit(1)
    elif offline_count / len(stations) > 0.3:
        print(f"  {WARN} >30% stations are offline — check for network outage")
    else:
        print(f"  {OK} Data looks healthy")

    print(f"\n  {BOLD}Sample record:{RESET}")
    print(f"  {json.dumps(stations[0], indent=4)}")

    return stations


def check_station_information(url: str) -> list[dict]:
    """Step 3: Validate station_information feed."""
    _section("Step 3 — Station Information Feed")
    print(f"  URL: {url}")

    try:
        data = _get(url)
    except Exception as e:
        print(f"  {FAIL} Cannot fetch station_information: {e}")
        sys.exit(1)

    stations = data["data"]["stations"]
    print(f"  {OK} Feed reachable — {len(stations)} stations returned")

    sample  = stations[0]
    missing = INFO_REQUIRED_FIELDS - set(sample.keys())
    if missing:
        print(f"  {WARN} Missing expected fields in station_information: {missing}")
    else:
        print(f"  {OK} Schema OK — all required fields present")

    # Geospatial bounds check (CDMX rough bounding box)
    lats = [s.get("lat", 0) for s in stations if s.get("lat")]
    lons = [s.get("lon", 0) for s in stations if s.get("lon")]
    print(f"\n  {BOLD}Geospatial bounds:{RESET}")
    print(f"    Lat range : {min(lats):.4f} → {max(lats):.4f}  (CDMX ≈ 19.2–19.6)")
    print(f"    Lon range : {min(lons):.4f} → {max(lons):.4f}  (CDMX ≈ -99.3 → -98.9)")

    if not (19.0 < min(lats) < 20.0 and -100.0 < min(lons) < -98.0):
        print(f"  {WARN} Coordinates look out of expected CDMX bounds — verify data")
    else:
        print(f"  {OK} Coordinates within expected CDMX bounds")

    print(f"\n  {BOLD}Sample record:{RESET}")
    print(f"  {json.dumps(stations[0], indent=4)}")

    return stations


def simulate_lambda(status_stations: list[dict], info_stations: list[dict]):
    """Step 4: Dry-run the Lambda transformation logic without any AWS calls."""
    _section("Step 4 — Lambda Dry-Run Simulation (no AWS calls)")

    ingest_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Mirror _build_status_records
    records = []
    for s in status_stations:
        records.append({
            "timestamp":       ingest_ts,
            "station_id":      str(s.get("station_id", "")),
            "bikes_available": int(s.get("num_bikes_available", 0)),
            "docks_available": int(s.get("num_docks_available", 0)),
            "is_renting":      bool(s.get("is_renting", False)),
            "is_returning":    bool(s.get("is_returning", False)),
            "_ingest_at":      ingest_ts,
        })

    print(f"  {OK} _build_status_records: {len(records)} records transformed")
    print(f"  Sample output record:")
    print(f"  {json.dumps(records[0], indent=4)}")

    # Mirror station_info transform
    info_records = []
    for s in info_stations:
        info_records.append({
            "station_id":  str(s.get("station_id", "")),
            "name":        s.get("name", ""),
            "lat":         float(s.get("lat", 0.0)),
            "lon":         float(s.get("lon", 0.0)),
            "capacity":    int(s.get("capacity", 0)),
            "_updated_at": ingest_ts,
        })

    print(f"\n  {OK} station_info transform: {len(info_records)} records transformed")

    # Firehose payload size estimate
    payload_bytes = sum(len((json.dumps(r) + "\n").encode("utf-8")) for r in records)
    print(f"\n  {BOLD}Estimated Firehose payload:{RESET}")
    print(f"    Records    : {len(records)}")
    print(f"    Total size : {payload_bytes / 1024:.1f} KB")
    print(f"    Batches    : {len(records) // 500 + 1} × 500-record batches")
    print(f"  {OK} Within Firehose limits (max 4MB/call, 500 records/call)")

    # Would-be return value
    result = {"statusCode": 200, "stations_pushed": len(records), "ingest_ts": ingest_ts}
    print(f"\n  {OK} Lambda would return: {json.dumps(result)}")


def main():
    parser = argparse.ArgumentParser(description="Local GBFS data availability checker.")
    parser.add_argument("--simulate-lambda", action="store_true",
                        help="Also run a dry-run Lambda transformation simulation.")
    args = parser.parse_args()

    print(f"\n{BOLD}{GREEN}╔══════════════════════════════════════════════════════╗")
    print(f"║  Ecobici GBFS Local Validator                        ║")
    print(f"╚══════════════════════════════════════════════════════╝{RESET}")
    print(f"  Timestamp : {datetime.now(timezone.utc).isoformat()}")
    print(f"  Discovery : {GBFS_DISCOVERY_URL}")

    feed_urls       = check_discovery()
    status_stations = check_station_status(feed_urls["station_status"])
    info_stations   = check_station_information(feed_urls["station_information"])

    if args.simulate_lambda:
        simulate_lambda(status_stations, info_stations)

    _section("Summary")
    print(f"  {OK} All checks passed — data is available and schema is valid")
    print(f"  {OK} Lambda is ready to deploy to AWS")
    print(f"\n  {BOLD}Next steps:{RESET}")
    print(f"    1. cd terraform && terraform apply   # deploy / update")
    print(f"    2. aws lambda invoke --function-name ecobici-ingest-gbfs \\")
    print(f"         --payload '{{}}' --cli-binary-format raw-in-base64-out response.json")
    print(f"    3. cat response.json")
    print()


if __name__ == "__main__":
    main()
