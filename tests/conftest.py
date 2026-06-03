"""
conftest.py — Shared pytest fixtures for Ecobici Lambda tests.

All AWS interactions are mocked via moto so tests run fully offline.
"""

import json
import os
import sys

import boto3
import pytest
from moto import mock_aws

# ---------------------------------------------------------------------------
# Make src/lambdas importable without installing a package
# ---------------------------------------------------------------------------
REPO_ROOT   = os.path.dirname(os.path.dirname(__file__))
LAMBDA_DIR  = os.path.join(REPO_ROOT, "src", "lambdas")
BACKFILL_DIR = os.path.join(REPO_ROOT, "src", "backfill")

for path in (LAMBDA_DIR, BACKFILL_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)


# ---------------------------------------------------------------------------
# Constants that mirror Terraform outputs
# ---------------------------------------------------------------------------
AWS_REGION              = "us-east-1"
S3_BUCKET               = "test-ecobici-datalake"
FIREHOSE_STATION_STREAM = "ecobici-station-status"
FIREHOSE_WEATHER_STREAM = "ecobici-weather"
GBFS_DISCOVERY_URL      = "https://gbfs.mex.lyftbikes.com/gbfs/gbfs.json"
WEATHER_SECRET_NAME     = "ecobici/weather_api"
SSM_REFRESH_PARAM       = "/ecobici/last_station_info_refresh"
GLUE_DATABASE           = "ecobici_lake"
ATHENA_WORKGROUP        = "ecobici-workgroup"
SNAPSHOT_DAYS           = "7"

WEATHER_SECRET_VALUE = json.dumps({
    "url":     "https://smn.conagua.gob.mx/tools/GUI/webservices/",
    "api_key": "test-weather-key",
})


# ---------------------------------------------------------------------------
# Environment variable injection (applied before any Lambda module is imported)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    """Set required env vars and fake AWS credentials for every test."""
    monkeypatch.setenv("AWS_DEFAULT_REGION",     AWS_REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID",      "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY",  "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN",     "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN",      "testing")

    # ingest_gbfs vars — no secret needed, uses public GBFS URL
    monkeypatch.setenv("GBFS_DISCOVERY_URL",      GBFS_DISCOVERY_URL)
    monkeypatch.setenv("FIREHOSE_STREAM_NAME",    FIREHOSE_STATION_STREAM)
    monkeypatch.setenv("S3_BUCKET",               S3_BUCKET)
    monkeypatch.setenv("SSM_REFRESH_PARAM",       SSM_REFRESH_PARAM)
    monkeypatch.setenv("GLUE_DATABASE",           GLUE_DATABASE)
    monkeypatch.setenv("ATHENA_WORKGROUP",        ATHENA_WORKGROUP)

    # ingest_weather vars
    monkeypatch.setenv("WEATHER_SECRET_NAME",     WEATHER_SECRET_NAME)

    # maintenance vars
    monkeypatch.setenv("SNAPSHOT_DAYS",           SNAPSHOT_DAYS)


# ---------------------------------------------------------------------------
# Moto-backed AWS resource fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def aws_mock():
    """Start the global moto mock (covers all AWS services used)."""
    with mock_aws():
        yield


@pytest.fixture
def s3_bucket(aws_mock):
    """Create the test S3 bucket."""
    s3 = boto3.client("s3", region_name=AWS_REGION)
    s3.create_bucket(Bucket=S3_BUCKET)
    return s3


@pytest.fixture
def secrets(aws_mock):
    """Seed Secrets Manager with the weather API secret.
    (No GBFS secret — the Lyft/Ecobici feed is 100% public.)
    """
    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    client.create_secret(Name=WEATHER_SECRET_NAME, SecretString=WEATHER_SECRET_VALUE)
    return client


@pytest.fixture
def ssm(aws_mock):
    """Create the SSM parameter with a past date (triggers daily refresh)."""
    client = boto3.client("ssm", region_name=AWS_REGION)
    client.put_parameter(Name=SSM_REFRESH_PARAM, Value="1970-01-01", Type="String")
    return client


@pytest.fixture
def firehose_station(aws_mock, s3_bucket):
    """
    Create the station-status Firehose delivery stream backed by the test S3 bucket.
    moto supports extended_s3 delivery via the S3 bucket fixture.
    """
    iam  = boto3.client("iam",      region_name=AWS_REGION)
    fh   = boto3.client("firehose", region_name=AWS_REGION)

    # Minimal IAM role for Firehose (moto doesn't validate policies)
    role = iam.create_role(
        RoleName="test-firehose-role",
        AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": []}),
    )

    fh.create_delivery_stream(
        DeliveryStreamName=FIREHOSE_STATION_STREAM,
        DeliveryStreamType="DirectPut",
        ExtendedS3DestinationConfiguration={
            "RoleARN":          role["Role"]["Arn"],
            "BucketARN":        f"arn:aws:s3:::{S3_BUCKET}",
            "Prefix":           "raw/station_status/",
            "BufferingHints":   {"SizeInMBs": 5, "IntervalInSeconds": 60},
            "CompressionFormat": "UNCOMPRESSED",
        },
    )
    return fh


@pytest.fixture
def firehose_weather(aws_mock, s3_bucket):
    """Create the weather Firehose delivery stream."""
    iam = boto3.client("iam",      region_name=AWS_REGION)
    fh  = boto3.client("firehose", region_name=AWS_REGION)

    role = iam.create_role(
        RoleName="test-firehose-weather-role",
        AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": []}),
    )
    fh.create_delivery_stream(
        DeliveryStreamName=FIREHOSE_WEATHER_STREAM,
        DeliveryStreamType="DirectPut",
        ExtendedS3DestinationConfiguration={
            "RoleARN":          role["Role"]["Arn"],
            "BucketARN":        f"arn:aws:s3:::{S3_BUCKET}",
            "Prefix":           "raw/weather/",
            "BufferingHints":   {"SizeInMBs": 5, "IntervalInSeconds": 60},
            "CompressionFormat": "UNCOMPRESSED",
        },
    )
    return fh
