# ECOBICI CDMX: Spatio-Temporal ML Data Lake Architecture

## 1. Project Role & Objective

Act as a Principal AWS Data Engineer and Machine Learning Architect. Your goal is to build an enterprise-grade, Infrastructure-as-Code (Terraform) Data Lake on AWS. This system ingests real-time bike-sharing data (Ecobici CDMX) and institutional weather data to serve as the foundational Feature Store for a Spatio-Temporal Transformer model.

**Core Directives (Ultra-Low Cost & Serverless):**

* **100% Serverless:** Use S3, AWS Glue, Athena, EventBridge, Lambda, and Kinesis Data Firehose. NO always-on databases (RDS/Redshift) and NO EC2 instances.
* **Cost-Trap Avoidance:** Do NOT deploy Lambda functions inside a VPC. They only need to access public APIs and AWS endpoints. This avoids provisioning costly NAT Gateways.
* **Table Format:** All tables MUST use **Apache Iceberg** via Athena for ACID transactions, schema evolution, and Time Travel.
* **Security & Credentials:** You MUST use **AWS Secrets Manager** to store the Ecobici API URL/Keys and the institutional weather API credentials.
* **IAM Least Privilege:** All IAM policies must strictly follow the principle of least privilege. Only grant access to the specific resources (S3 prefixes, specific Secrets, Firehose streams) needed for each Lambda. NO wildcard `*` permissions for destructive actions.

## 2. Target Directory Structure (GitHub Repository)

Generate the project using the following structure, prepared for Git version control:

```text
├── .github/
│   └── workflows/
│       └── deploy.yml        # CI/CD pipeline for Terraform & Lambda deployments
├── terraform/                # IaC definitions
│   ├── main.tf               # Provider, S3 (Versioning ON), Secrets Manager
│   ├── glue_athena.tf        # Glue Catalog & Iceberg Table definitions
│   ├── firehose.tf           # Kinesis Firehose streams (Buffer: 60s / 5MB)
│   ├── lambda.tf             # Lambda functions (Timeout: 60s), EventBridge, IAM
│   └── monitoring.tf         # CloudWatch Alarms & SNS Topics
├── src/                      # Application code (Python 3.10+)
│   ├── lambdas/
│   │   ├── ingest_gbfs.py    # Fetches Ecobici API (5 min) & Static info (Daily)
│   │   ├── ingest_weather.py # Fetches Institutional Weather (10 min)
│   │   └── maintenance.py    # Iceberg vacuum/optimize script (Weekly)
│   ├── backfill/
│   │   └── load_history.py   # One-off script for 2023+ CSVs
│   └── sql/
│       ├── rollups.sql       # Athena CTAS for hourly aggregations
│       └── views.sql         # Geospatial materialized views
├── .gitignore                # Ignores .env, .terraform/, tfstate, __pycache__, etc.
├── .env.example              # Template for API keys (for local testing)
└── README.md                 # Complete documentation and setup steps

```

## 3. Data Schema & Engineering Requirements

### A. Ecobici GBFS Pipeline (Lambda -> Firehose -> Iceberg)

* **`raw_station_status` (5-min interval):** Partitioned by `day(timestamp)`. Fields: `timestamp`, `station_id`, `bikes_available`, `docks_available`, `is_renting`, `is_returning`.
* **`ecobici_station_info` (Daily update):** Fields: `station_id`, `name`, `lat`, `lon`, `capacity`.

### B. Institutional Weather Pipeline (Lambda -> Firehose -> Iceberg)

*DO NOT use generic APIs (e.g., OpenWeather). Fetch from SMN, REDMET, or OH-UNAM.*

* **`weather_observations` (10-min interval):** Partitioned by `day(timestamp)`. Must include in-memory micro-cleaning (reject out-of-bounds temps, forward-fill missing data up to 30 mins). Fields: `timestamp`, `station_id`, `temp_c`, `precip_mm`.
* **Geospatial Dynamic Mapping (Athena):** Create a Materialized View (`vw_ecobici_weather_mapping`) using `ST_Distance(ST_Point(lon, lat))` to automatically map every Ecobici station to its nearest Weather station dynamically.

## 4. Analytics & Heuristic Logic (Athena SQL)

Create an **Hourly Rollup Table** (`hourly_station_status`) partitioned by `month`.
**Heuristic Malfunction Detection:**
A station is flagged as `is_heuristically_broken = true` if:

1. `is_renting` and `is_returning` are FALSE natively in the feed, OR...
2. `bikes_available > 0` AND the count has not changed by ± 1 for 3 consecutive hours during peak time (07:00 - 21:00), despite neighboring stations showing high turnover.

## 5. DataOps, FinOps, and Backfilling

### A. Historical Backfilling (2023 - Present)

* Write a local Python script (`load_history.py`) using Pandas to download monthly Ecobici CSVs.
* **Transformation:** Convert trip data (origin-destination) into availability deltas to match the `hourly_station_status` schema.
* **Cost/Privacy Optimization:** The script MUST drop all PII and user demographic columns (age, gender, hashed IDs) before writing to S3 to save storage costs.
* **Error Handling:** Use `to_numeric(errors='coerce')` to silently drop corrupted rows, logging the drop count.

### B. Iceberg Maintenance (FinOps)

* Create a Lambda triggered weekly to run Athena SQL commands:
1. `OPTIMIZE table_name REWRITE DATA USING BIN_PACK;`
2. `ALTER TABLE table_name EXECUTE expire_snapshots(retention_threshold => '7d');`



### C. Observability (CloudWatch & SNS)

* **Ingestion Alarm:** Trigger SNS Email if the `ingest_gbfs` Lambda fails 3 consecutive times.
* **Data Drift Alarm:** Trigger SNS Email if a daily Athena query detects `SUM(bikes_available) == 0` across the network.

## 6. End-Goal: ML Tensor Context (For Reference)

Ensure schemas support fast transformation into a 4D Tensor: `[Batch, Time_Steps(24), Nodes(687), Features]`. The final model will use a Spatial Adjacency Mask (radius < 3km).

## 7. Action Plan

Begin by executing the following steps sequentially. Wait for my confirmation after each step:

1. Generate the **`.gitignore`** file specifically tailored for Python and Terraform.
2. Generate the comprehensive **`README.md`** file documenting all steps to deploy and run this repository.
3. Generate the **Terraform configuration files** (Provider, S3, IAM, Secrets Manager, and Glue Databases).