# 🚲 Ecobici CDMX — Spatio-Temporal ML Data Lake

> **Enterprise-grade, 100% serverless Data Lake on AWS, built with Terraform and Apache Iceberg.**
> Ingests real-time bike-sharing telemetry and institutional weather data to serve as the foundational Feature Store for a Spatio-Temporal Transformer model.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Repository Structure](#2-repository-structure)
3. [Prerequisites](#3-prerequisites)
4. [Configuration & Secrets](#4-configuration--secrets)
5. [Deploying the Infrastructure (Terraform)](#5-deploying-the-infrastructure-terraform)
6. [Lambda Functions](#6-lambda-functions)
7. [Data Schemas](#7-data-schemas)
8. [Analytics & Heuristic Logic](#8-analytics--heuristic-logic)
9. [Historical Backfilling](#9-historical-backfilling)
10. [Iceberg Maintenance (FinOps)](#10-iceberg-maintenance-finops)
11. [Observability & Alerting](#11-observability--alerting)
12. [CI/CD Pipeline](#12-cicd-pipeline)
13. [ML Tensor Context](#13-ml-tensor-context)
14. [Cost Optimization Notes](#14-cost-optimization-notes)
15. [Teardown](#15-teardown)

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         AWS Cloud (Serverless)                       │
│                                                                       │
│  EventBridge (5 min) ──► Lambda: ingest_gbfs.py                      │
│                                    │                                  │
│                                    ▼                                  │
│                          Kinesis Data Firehose                        │
│                          (Buffer: 60s / 5MB)                          │
│                                    │                                  │
│                                    ▼                                  │
│                          S3 Data Lake (Versioning ON)                 │
│                          └── raw/station_status/                      │
│                          └── raw/station_info/                        │
│                          └── raw/weather/                             │
│                          └── processed/hourly_rollups/                │
│                                    │                                  │
│                                    ▼                                  │
│                        AWS Glue Data Catalog                          │
│                        (Iceberg table metadata)                       │
│                                    │                                  │
│                                    ▼                                  │
│                          Amazon Athena (SQL)                          │
│                          ├── Rollups (CTAS)                           │
│                          ├── Geospatial views                         │
│                          └── Heuristic malfunction detection          │
│                                                                       │
│  EventBridge (10 min) ─► Lambda: ingest_weather.py                   │
│  EventBridge (Weekly) ─► Lambda: maintenance.py                      │
│                                                                       │
│  CloudWatch Alarms ──────────────► SNS ──► Email Alerts              │
│  AWS Secrets Manager ────────────► Lambdas (API credentials)         │
└─────────────────────────────────────────────────────────────────────┘
```

**Key design decisions:**
- **No VPC for Lambdas** — avoids NAT Gateway costs; all targets are public APIs or AWS service endpoints.
- **Apache Iceberg** via Athena for ACID transactions, time travel, and schema evolution without table rewrites.
- **Kinesis Data Firehose** as the write buffer — decouples ingestion from storage, handles back-pressure automatically.

---

## 2. Repository Structure

```
├── .github/
│   └── workflows/
│       └── deploy.yml          # CI/CD: Terraform plan/apply + Lambda packaging
├── terraform/
│   ├── main.tf                 # Provider, backend (S3), S3 bucket (versioning ON), Secrets Manager
│   ├── glue_athena.tf          # Glue Database + Iceberg table definitions
│   ├── firehose.tf             # Kinesis Firehose streams (60s / 5MB buffer)
│   ├── lambda.tf               # Lambda functions, EventBridge rules, IAM roles/policies
│   └── monitoring.tf           # CloudWatch Alarms + SNS Topics
├── src/
│   ├── lambdas/
│   │   ├── ingest_gbfs.py      # Ecobici GBFS ingestor (every 5 min)
│   │   ├── ingest_weather.py   # Institutional weather ingestor (every 10 min)
│   │   └── maintenance.py      # Iceberg OPTIMIZE + expire_snapshots (weekly)
│   ├── backfill/
│   │   └── load_history.py     # One-off historical CSV loader (2023–present)
│   └── sql/
│       ├── rollups.sql         # Athena CTAS: hourly_station_status aggregation
│       └── views.sql           # Geospatial materialized view: vw_ecobici_weather_mapping
├── .env.example                # Template for local dev (never commit .env)
├── .gitignore
└── README.md
```

---

## 3. Prerequisites

### Local toolchain

| Tool | Version | Install |
|---|---|---|
| **Terraform** | ≥ 1.7 | [terraform.io/downloads](https://developer.hashicorp.com/terraform/downloads) |
| **Python** | ≥ 3.10 | `pyenv install 3.10.14` |
| **AWS CLI** | ≥ 2.15 | [aws.amazon.com/cli](https://aws.amazon.com/cli/) |
| **pip** | latest | `pip install --upgrade pip` |

### AWS IAM permissions (deploy user)

The IAM user/role running Terraform needs the following managed policies attached:
- `AmazonS3FullAccess`
- `AWSGlueConsoleFullAccess`
- `AmazonKinesisFirehoseFullAccess`
- `AWSLambda_FullAccess`
- `AmazonEventBridgeFullAccess`
- `SecretsManagerReadWrite`
- `IAMFullAccess` *(scoped to resource prefix `ecobici-*`)*
- `CloudWatchFullAccess`
- `AmazonAthenaFullAccess`
- `AmazonSNSFullAccess`

> For production, replace managed policies with a custom least-privilege policy scoped to `ecobici-*` resources.

### Python dependencies (local dev)

```bash
python -m venv .venv
source .venv/bin/activate
pip install boto3 pandas requests
```

---

## 4. Configuration & Secrets

### Local environment (development only)

Copy the template and fill in your values:

```bash
cp .env.example .env
```

`.env.example` contents:

```dotenv
# Ecobici GBFS API
ECOBICI_API_BASE_URL=https://gbfs.mex.lyft.com/gbfs/2.3/mex_mexico_city
ECOBICI_API_KEY=your_ecobici_api_key_here

# Institutional Weather (SMN / REDMET / OH-UNAM)
WEATHER_API_URL=https://smn.conagua.gob.mx/tools/GUI/webservices/
WEATHER_API_KEY=your_weather_api_key_here

# AWS (for local boto3 calls — use IAM roles in CI/CD)
AWS_REGION=us-east-1
AWS_PROFILE=ecobici-de-01
```

### AWS Secrets Manager (production)

Credentials are **never passed as Lambda environment variables**. They are fetched at runtime from Secrets Manager. Terraform provisions two secrets:

| Secret Name | Contents |
|---|---|
| `ecobici/gbfs_api` | `{"url": "...", "api_key": "..."}` |
| `ecobici/weather_api` | `{"url": "...", "api_key": "..."}` |

After `terraform apply`, populate the secret values:

```bash
aws secretsmanager put-secret-value \
  --secret-id ecobici/gbfs_api \
  --secret-string '{"url":"https://gbfs.mex.lyft.com/gbfs/2.3/mex_mexico_city","api_key":"YOUR_KEY"}'

aws secretsmanager put-secret-value \
  --secret-id ecobici/weather_api \
  --secret-string '{"url":"https://smn.conagua.gob.mx/tools/GUI/webservices/","api_key":"YOUR_KEY"}'
```

---

## 5. Deploying the Infrastructure (Terraform)

### 5.1 Configure AWS credentials

```bash
aws configure --profile ecobici-de-01
# Enter: Access Key ID, Secret Access Key, Region (us-east-1), Output format (json)
```

### 5.2 Set Terraform variables

```bash
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
# Edit terraform.tfvars with your values:
#   aws_region        = "us-east-1"
#   project_prefix    = "ecobici"
#   alert_email       = "your@email.com"
#   s3_bucket_name    = "ecobici-datalake-<account_id>"
```

### 5.3 Initialize and deploy

```bash
cd terraform

# Download providers and modules
terraform init

# Review the execution plan (no changes are applied)
terraform plan -out=ecobici.tfplan

# Apply the plan (requires confirmation)
terraform apply ecobici.tfplan
```

Expected output resources created (~25–30 resources):
- 1× S3 Bucket (versioning enabled, lifecycle rules)
- 2× Secrets Manager secrets
- 1× Glue Database
- 4× Glue/Iceberg tables
- 2× Kinesis Firehose streams
- 3× Lambda functions
- 3× EventBridge rules
- 3× IAM roles + inline policies
- 2× CloudWatch Alarms
- 1× SNS Topic + Email subscription

### 5.4 Confirm SNS email subscription

After apply, check your inbox for a **"AWS Notification - Subscription Confirmation"** email and click the confirmation link.

### 5.5 Verify deployment

```bash
# List Lambda functions
aws lambda list-functions --query 'Functions[?starts_with(FunctionName, `ecobici`)].FunctionName'

# Manually invoke the GBFS ingestor for a smoke test
aws lambda invoke \
  --function-name ecobici-ingest-gbfs \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  response.json && cat response.json
```

---

## 6. Lambda Functions

### `ingest_gbfs.py` — Ecobici GBFS Ingestor

- **Trigger:** EventBridge every **5 minutes**
- **Timeout:** 60 seconds | **Memory:** 256 MB
- **What it does:**
  1. Fetches `station_status.json` from the GBFS feed (credentials from Secrets Manager).
  2. Normalizes and enriches each record with an ingestion `timestamp`.
  3. Sends records as newline-delimited JSON to the `ecobici-station-status` Firehose stream.
  4. Once daily (checked via SSM Parameter), fetches `station_information.json` and updates `ecobici_station_info`.

### `ingest_weather.py` — Institutional Weather Ingestor

- **Trigger:** EventBridge every **10 minutes**
- **Timeout:** 60 seconds | **Memory:** 256 MB
- **What it does:**
  1. Fetches observations from SMN/REDMET/OH-UNAM API.
  2. Applies in-memory micro-cleaning:
     - Rejects temperature readings outside `[-10°C, 50°C]`.
     - Forward-fills missing readings up to **30 minutes**.
  3. Sends cleaned records to the `ecobici-weather` Firehose stream.

### `maintenance.py` — Iceberg Table Maintenance

- **Trigger:** EventBridge every **Sunday at 02:00 UTC**
- **Timeout:** 300 seconds | **Memory:** 512 MB
- **What it does:**
  1. Runs `OPTIMIZE ... REWRITE DATA USING BIN_PACK` on all Iceberg tables.
  2. Runs `ALTER TABLE ... EXECUTE expire_snapshots(retention_threshold => '7d')`.
  3. Logs results and byte savings to CloudWatch.

---

## 7. Data Schemas

### `raw_station_status` (Iceberg, partitioned by `day(timestamp)`)

| Column | Type | Description |
|---|---|---|
| `timestamp` | `TIMESTAMP` | Ingestion time (UTC) |
| `station_id` | `STRING` | Ecobici station identifier |
| `bikes_available` | `INT` | Available bikes |
| `docks_available` | `INT` | Available docking slots |
| `is_renting` | `BOOLEAN` | Station accepting bike pickups |
| `is_returning` | `BOOLEAN` | Station accepting returns |
| `_ingest_at` | `TIMESTAMP` | Lambda processing timestamp |

### `ecobici_station_info` (Iceberg, daily SCD Type 1)

| Column | Type | Description |
|---|---|---|
| `station_id` | `STRING` | Unique station ID |
| `name` | `STRING` | Human-readable station name |
| `lat` | `DOUBLE` | Latitude (WGS84) |
| `lon` | `DOUBLE` | Longitude (WGS84) |
| `capacity` | `INT` | Total bike docks |
| `_updated_at` | `TIMESTAMP` | Last info refresh |

### `weather_observations` (Iceberg, partitioned by `day(timestamp)`)

| Column | Type | Description |
|---|---|---|
| `timestamp` | `TIMESTAMP` | Observation time (UTC) |
| `station_id` | `STRING` | Weather station ID |
| `temp_c` | `DOUBLE` | Temperature °C (validated) |
| `precip_mm` | `DOUBLE` | Precipitation mm |
| `_is_filled` | `BOOLEAN` | `true` if forward-filled |

### `hourly_station_status` (Iceberg, partitioned by `month(hour)`)

Aggregated via Athena CTAS rollup. Includes `is_heuristically_broken` flag.

---

## 8. Analytics & Heuristic Logic

### Hourly Rollup

Run `src/sql/rollups.sql` via Athena after the Iceberg tables have been populated. The CTAS materializes `hourly_station_status`.

### Heuristic Malfunction Detection

A station is flagged `is_heuristically_broken = true` if **either** condition is met:

1. **Native flag:** `is_renting = false AND is_returning = false` in the raw feed.
2. **Frozen count:** `bikes_available > 0` **AND** the count has not changed by ±1 over **3 consecutive hours** during peak hours (07:00–21:00 local time), **AND** neighboring stations (within 3 km) show high bike turnover.

### Geospatial Weather Mapping

`src/sql/views.sql` creates `vw_ecobici_weather_mapping` using Athena's built-in geospatial functions:

```sql
ST_Distance(
  ST_Point(e.lon, e.lat),
  ST_Point(w.lon, w.lat)
) AS distance_m
```

This view auto-assigns each Ecobici station to its nearest weather station dynamically — no static join table needed.

---

## 9. Historical Backfilling

Downloads and transforms Ecobici monthly CSV trip data (2023–present) into the `hourly_station_status` schema.

```bash
cd src/backfill

# Install dependencies
pip install pandas requests tqdm

# Run the backfill (will take 10–30 min depending on data volume)
python load_history.py \
  --start-month 2023-01 \
  --end-month 2026-05 \
  --s3-bucket ecobici-datalake-<account_id> \
  --s3-prefix processed/hourly_backfill/
```

**Key transformations applied:**
- Converts origin-destination trip records → per-station availability **deltas**.
- Drops all PII columns: `user_age`, `user_gender`, `user_id`, hashed identifiers.
- Uses `pd.to_numeric(errors='coerce')` to silently drop corrupted rows (drop count is logged).
- Writes output as **Parquet** to S3; Glue Crawler picks up the new partitions automatically.

---

## 10. Iceberg Maintenance (FinOps)

The `maintenance.py` Lambda runs automatically every Sunday. To trigger it manually:

```bash
aws lambda invoke \
  --function-name ecobici-maintenance \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  maintenance_response.json && cat maintenance_response.json
```

**Why this matters:**
- `OPTIMIZE ... BIN_PACK` merges many small Parquet files into optimal 128MB files → **reduces Athena scan costs by ~40–60%**.
- `expire_snapshots` removes old Iceberg snapshot metadata (7-day retention) → **reduces S3 storage costs**.

---

## 11. Observability & Alerting

| Alarm | Condition | Action |
|---|---|---|
| `ecobici-ingest-gbfs-failures` | Lambda fails 3 consecutive invocations | SNS Email |
| `ecobici-data-drift` | Daily Athena: `SUM(bikes_available) = 0` across network | SNS Email |

Dashboards are available in CloudWatch under the `EcobiciDataLake` namespace.

---

## 12. CI/CD Pipeline

The GitHub Actions workflow (`.github/workflows/deploy.yml`) runs on every push to `main`:

1. **Lint & test** — `terraform fmt -check`, `terraform validate`, Python `ruff` linter.
2. **Package Lambdas** — zips each `src/lambdas/*.py` with its dependencies.
3. **Terraform plan** — posts the plan diff as a PR comment.
4. **Terraform apply** — runs only on direct pushes to `main` (not PRs).

**Required GitHub Secrets:**

| Secret | Value |
|---|---|
| `AWS_ACCESS_KEY_ID` | Deploy IAM user access key |
| `AWS_SECRET_ACCESS_KEY` | Deploy IAM user secret key |
| `AWS_REGION` | e.g., `us-east-1` |
| `TF_VAR_alert_email` | Email for SNS alerts |

---

## 13. ML Tensor Context

The schemas are designed to produce a **4D Tensor**: `[Batch, Time_Steps(24), Nodes(687), Features]`.

- **Time axis (24):** 24 hourly slots per day from `hourly_station_status`.
- **Node axis (687):** 687 Ecobici stations, identified by `station_id`.
- **Feature axis:** `[bikes_available, docks_available, temp_c, precip_mm, is_heuristically_broken]`.
- **Spatial Adjacency Mask:** built using `vw_ecobici_weather_mapping` with a `distance_m < 3000` filter to create the graph edges for the Spatio-Temporal Transformer.

---

## 14. Cost Optimization Notes

| Pattern | Saving |
|---|---|
| No VPC for Lambdas | Avoids ~$32/month NAT Gateway cost |
| Firehose 60s/5MB buffer | Fewer S3 PUTs → lower request costs |
| Iceberg BIN_PACK weekly | ~40–60% reduction in Athena data scanned |
| S3 Intelligent-Tiering | Auto-moves cold partitions to cheaper storage tiers |
| Athena result reuse | Enable query result reuse (5 min TTL) for repeated dashboard queries |
| expire_snapshots 7d | Eliminates orphaned Parquet files from S3 |

**Estimated monthly cost** (at current Ecobici scale, ~687 stations, 5-min polling):
- S3 storage: ~$0.50–2.00
- Firehose: ~$0.20–0.50
- Lambda: within free tier
- Athena: ~$1.00–3.00 (varies by query volume)
- **Total: < $10/month**

---

## 15. Teardown

To destroy all AWS resources:

```bash
cd terraform

# Preview what will be destroyed
terraform plan -destroy -out=destroy.tfplan

# Execute destruction (IRREVERSIBLE — all S3 data will be deleted)
terraform apply destroy.tfplan
```

> ⚠️ **Warning:** This deletes all S3 data. If you want to preserve historical data, manually copy the S3 bucket contents to another location before running destroy.

---

*Built with ❤️ using AWS Serverless, Apache Iceberg, and Terraform — optimized for ultra-low cost and ML-readiness.*
