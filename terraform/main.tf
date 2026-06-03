##############################################################################
# Provider & Backend
##############################################################################

terraform {
  required_version = ">= 1.7.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.50"
    }
  }

  # Remote state stored in the same S3 bucket (bootstrap manually once).
  # To bootstrap: create the bucket & DynamoDB table manually, then init.
  backend "s3" {
    key     = "ecobici/terraform.tfstate"
    encrypt = true
    # bucket and region are supplied at init time:
    #   terraform init -backend-config="bucket=ecobici-datalake-<account_id>" \
    #                  -backend-config="region=us-east-1" \
    #                  -backend-config="dynamodb_table=ecobici-tf-locks"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = var.tags
  }
}

##############################################################################
# Current account data
##############################################################################

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

##############################################################################
# S3 Data Lake Bucket
##############################################################################

resource "aws_s3_bucket" "datalake" {
  bucket        = var.s3_bucket_name
  force_destroy = false # Protect against accidental terraform destroy

  tags = {
    Name = var.s3_bucket_name
  }
}

resource "aws_s3_bucket_versioning" "datalake" {
  bucket = aws_s3_bucket.datalake.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "datalake" {
  bucket = aws_s3_bucket.datalake.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "datalake" {
  bucket = aws_s3_bucket.datalake.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Intelligent-Tiering: automatically move cold objects to cheaper tiers
resource "aws_s3_bucket_intelligent_tiering_configuration" "datalake" {
  bucket = aws_s3_bucket.datalake.id
  name   = "ecobici-tiering"

  tiering {
    access_tier = "ARCHIVE_ACCESS"
    days        = 90
  }
}

# S3 Lifecycle: expire Athena result objects after 30 days
resource "aws_s3_bucket_lifecycle_configuration" "datalake" {
  bucket = aws_s3_bucket.datalake.id

  rule {
    id     = "expire-athena-results"
    status = "Enabled"

    filter {
      prefix = var.athena_results_prefix
    }

    expiration {
      days = 30
    }
  }
}

##############################################################################
# Athena Workgroup
##############################################################################

resource "aws_athena_workgroup" "main" {
  name  = "${var.project_prefix}-workgroup"
  state = "ENABLED"

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true

    result_configuration {
      output_location = "s3://${aws_s3_bucket.datalake.id}/${var.athena_results_prefix}"

      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }

    engine_version {
      selected_engine_version = "Athena engine version 3"
    }
  }

  tags = {
    Name = "${var.project_prefix}-workgroup"
  }
}

##############################################################################
# Secrets Manager — API Credentials
##############################################################################

# Note: ecobici/gbfs_api secret removed — the Lyft/Ecobici GBFS feed is
# 100% public and requires no authentication. The discovery URL is passed
# directly as a Lambda environment variable.


# Note: ecobici/weather_api secret removed — the Open-Meteo weather API is
# 100% public and requires no authentication. Station coordinates are read
# from the daily station_info file in S3 (written by ingest_gbfs Lambda).


##############################################################################
# SSM Parameter — Daily station_info refresh tracking
##############################################################################

resource "aws_ssm_parameter" "last_station_info_refresh" {
  name  = "/${var.project_prefix}/last_station_info_refresh"
  type  = "String"
  value = "1970-01-01"

  tags = {
    Name = "${var.project_prefix}-last-station-info-refresh"
  }

  lifecycle {
    ignore_changes = [value]
  }
}
