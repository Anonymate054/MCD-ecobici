"""
maintenance.py — Iceberg Table Maintenance Lambda
==================================================
Triggered weekly (every Sunday at 02:00 UTC) by EventBridge.

For each Iceberg table in the catalog, runs:
  1. OPTIMIZE ... REWRITE DATA USING BIN_PACK
     → Merges small Parquet files into optimal 128MB files.
     → Reduces Athena scan costs by 40–60%.
  2. ALTER TABLE ... EXECUTE expire_snapshots(retention_threshold => 'Nd')
     → Removes snapshot metadata older than SNAPSHOT_DAYS.
     → Reduces orphaned Parquet files in S3.

Publishes a custom CloudWatch metric `EcobiciDataLake/MaintenanceDurationMs`
for each table so you can track execution trends over time.

Env vars (injected by Terraform):
  GLUE_DATABASE    - Glue catalog database name
  ATHENA_WORKGROUP - Athena workgroup name
  S3_BUCKET        - S3 data lake bucket (used for results path)
  SNAPSHOT_DAYS    - Snapshot retention in days (default: 7)
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------
athena_client = boto3.client("athena")
cw_client     = boto3.client("cloudwatch")

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
GLUE_DATABASE    = os.environ["GLUE_DATABASE"]
ATHENA_WORKGROUP = os.environ["ATHENA_WORKGROUP"]
S3_BUCKET        = os.environ["S3_BUCKET"]
SNAPSHOT_DAYS    = int(os.environ.get("SNAPSHOT_DAYS", "7"))

# Tables to maintain (must match glue_athena.tf)
MANAGED_TABLES = [
    "raw_station_status",
    "ecobici_station_info",
    "weather_observations",
    "hourly_station_status",
]

POLL_INTERVAL_SECS = 5
MAX_WAIT_SECS      = 270   # Stay within 300s Lambda timeout


# ---------------------------------------------------------------------------
# Athena helpers
# ---------------------------------------------------------------------------

def _run_athena_query(sql: str, description: str) -> str:
    """Submit an Athena query and return the QueryExecutionId."""
    logger.info("Starting Athena query [%s]: %.120s...", description, sql)
    response = athena_client.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": GLUE_DATABASE},
        WorkGroup=ATHENA_WORKGROUP,
    )
    return response["QueryExecutionId"]


def _wait_for_query(execution_id: str, description: str) -> dict:
    """
    Poll until the query succeeds or fails.
    Returns the final GetQueryExecution response dict.
    Raises RuntimeError on failure or timeout.
    """
    elapsed = 0
    while elapsed < MAX_WAIT_SECS:
        time.sleep(POLL_INTERVAL_SECS)
        elapsed += POLL_INTERVAL_SECS

        response = athena_client.get_query_execution(QueryExecutionId=execution_id)
        state    = response["QueryExecution"]["Status"]["State"]

        if state == "SUCCEEDED":
            stats = response["QueryExecution"].get("Statistics", {})
            scanned_bytes = stats.get("DataScannedInBytes", 0)
            logger.info(
                "[%s] Query SUCCEEDED (%.2f MB scanned, %ds elapsed)",
                description, scanned_bytes / 1_048_576, elapsed,
            )
            return response

        if state in ("FAILED", "CANCELLED"):
            reason = response["QueryExecution"]["Status"].get("StateChangeReason", "Unknown")
            raise RuntimeError(
                f"Athena query [{description}] {state}: {reason}"
            )

        logger.debug("[%s] Still running... state=%s (%ds elapsed)", description, state, elapsed)

    raise TimeoutError(
        f"Athena query [{description}] did not complete within {MAX_WAIT_SECS}s"
    )


def _publish_metric(table: str, operation: str, duration_ms: float, success: bool) -> None:
    """Publish a custom CloudWatch metric for maintenance tracking."""
    try:
        cw_client.put_metric_data(
            Namespace="EcobiciDataLake",
            MetricData=[
                {
                    "MetricName": "MaintenanceDurationMs",
                    "Dimensions": [
                        {"Name": "Table",     "Value": table},
                        {"Name": "Operation", "Value": operation},
                        {"Name": "Status",    "Value": "success" if success else "failure"},
                    ],
                    "Value": duration_ms,
                    "Unit":  "Milliseconds",
                    "Timestamp": datetime.now(timezone.utc),
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to publish CloudWatch metric: %s", exc)


# ---------------------------------------------------------------------------
# Maintenance operations
# ---------------------------------------------------------------------------

def _run_optimize(table: str) -> dict:
    """Run OPTIMIZE ... REWRITE DATA USING BIN_PACK for one table."""
    sql = f'OPTIMIZE "{GLUE_DATABASE}"."{table}" REWRITE DATA USING BIN_PACK'
    t0  = time.monotonic()

    try:
        exec_id  = _run_athena_query(sql, f"OPTIMIZE:{table}")
        result   = _wait_for_query(exec_id, f"OPTIMIZE:{table}")
        duration = (time.monotonic() - t0) * 1000
        _publish_metric(table, "OPTIMIZE", duration, success=True)
        return {"table": table, "operation": "OPTIMIZE", "status": "success", "duration_ms": round(duration)}
    except Exception as exc:
        duration = (time.monotonic() - t0) * 1000
        _publish_metric(table, "OPTIMIZE", duration, success=False)
        logger.error("OPTIMIZE failed for %s: %s", table, exc)
        return {"table": table, "operation": "OPTIMIZE", "status": "failed", "error": str(exc)}


def _run_expire_snapshots(table: str) -> dict:
    """Run ALTER TABLE ... EXECUTE expire_snapshots for one table."""
    sql = (
        f'ALTER TABLE "{GLUE_DATABASE}"."{table}" '
        f"EXECUTE expire_snapshots(retention_threshold => '{SNAPSHOT_DAYS}d')"
    )
    t0 = time.monotonic()

    try:
        exec_id  = _run_athena_query(sql, f"EXPIRE_SNAPSHOTS:{table}")
        result   = _wait_for_query(exec_id, f"EXPIRE_SNAPSHOTS:{table}")
        duration = (time.monotonic() - t0) * 1000
        _publish_metric(table, "EXPIRE_SNAPSHOTS", duration, success=True)
        return {"table": table, "operation": "EXPIRE_SNAPSHOTS", "status": "success", "duration_ms": round(duration)}
    except Exception as exc:
        duration = (time.monotonic() - t0) * 1000
        _publish_metric(table, "EXPIRE_SNAPSHOTS", duration, success=False)
        logger.error("expire_snapshots failed for %s: %s", table, exc)
        return {"table": table, "operation": "EXPIRE_SNAPSHOTS", "status": "failed", "error": str(exc)}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handler(event: Any, context: Any) -> dict:
    """Lambda entry point — run maintenance on all managed Iceberg tables."""
    logger.info(
        "Starting Iceberg maintenance on database '%s' (snapshot retention: %dd)",
        GLUE_DATABASE, SNAPSHOT_DAYS,
    )

    results = []
    failures = 0

    for table in MANAGED_TABLES:
        logger.info("--- Processing table: %s ---", table)

        # Step 1: OPTIMIZE (BIN_PACK)
        opt_result = _run_optimize(table)
        results.append(opt_result)
        if opt_result["status"] != "success":
            failures += 1

        # Step 2: expire_snapshots (run even if OPTIMIZE failed — independent operation)
        exp_result = _run_expire_snapshots(table)
        results.append(exp_result)
        if exp_result["status"] != "success":
            failures += 1

    summary = {
        "statusCode":     200 if failures == 0 else 207,
        "tables_processed": len(MANAGED_TABLES),
        "operations_total": len(results),
        "failures":         failures,
        "results":          results,
        "run_at":           datetime.now(timezone.utc).isoformat(),
    }

    logger.info(
        "Maintenance complete: %d tables, %d operations, %d failures",
        len(MANAGED_TABLES), len(results), failures,
    )
    logger.info("Summary: %s", json.dumps(summary, indent=2))

    if failures > 0:
        logger.warning(
            "%d maintenance operation(s) failed — check CloudWatch Logs for details.", failures
        )

    return summary
