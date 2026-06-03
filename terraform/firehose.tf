##############################################################################
# IAM — Firehose Delivery Role (shared by both streams)
##############################################################################

data "aws_iam_policy_document" "firehose_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["firehose.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "firehose_delivery" {
  name               = "${var.project_prefix}-firehose-delivery-role"
  assume_role_policy = data.aws_iam_policy_document.firehose_assume_role.json

  tags = {
    Name = "${var.project_prefix}-firehose-delivery-role"
  }
}

data "aws_iam_policy_document" "firehose_s3_access" {
  statement {
    sid    = "S3DataLakeWrite"
    effect = "Allow"

    actions = [
      "s3:AbortMultipartUpload",
      "s3:GetBucketLocation",
      "s3:GetObject",
      "s3:ListBucket",
      "s3:ListBucketMultipartUploads",
      "s3:PutObject",
    ]

    resources = [
      aws_s3_bucket.datalake.arn,
      "${aws_s3_bucket.datalake.arn}/raw/*",
    ]
  }

  statement {
    sid    = "GlueCatalogRead"
    effect = "Allow"

    actions = [
      "glue:GetTable",
      "glue:GetTableVersion",
      "glue:GetTableVersions",
    ]

    resources = [
      "arn:aws:glue:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:catalog",
      "arn:aws:glue:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:database/${aws_glue_catalog_database.ecobici.name}",
      "arn:aws:glue:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${aws_glue_catalog_database.ecobici.name}/*",
    ]
  }

  statement {
    sid    = "CloudWatchLogs"
    effect = "Allow"

    actions = [
      "logs:PutLogEvents",
      "logs:CreateLogStream",
    ]

    resources = [
      "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/kinesisfirehose/${var.project_prefix}-*:*",
    ]
  }
}

resource "aws_iam_role_policy" "firehose_s3_access" {
  name   = "firehose-s3-access"
  role   = aws_iam_role.firehose_delivery.id
  policy = data.aws_iam_policy_document.firehose_s3_access.json
}

##############################################################################
# CloudWatch Log Groups for Firehose error logging
##############################################################################

resource "aws_cloudwatch_log_group" "firehose_station_status" {
  name              = "/aws/kinesisfirehose/${var.project_prefix}-station-status"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "firehose_weather" {
  name              = "/aws/kinesisfirehose/${var.project_prefix}-weather"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_stream" "firehose_station_status_errors" {
  name           = "S3Delivery"
  log_group_name = aws_cloudwatch_log_group.firehose_station_status.name
}

resource "aws_cloudwatch_log_stream" "firehose_weather_errors" {
  name           = "S3Delivery"
  log_group_name = aws_cloudwatch_log_group.firehose_weather.name
}

##############################################################################
# Kinesis Data Firehose — Station Status Stream
##############################################################################

resource "aws_kinesis_firehose_delivery_stream" "station_status" {
  name        = "${var.project_prefix}-station-status"
  destination = "extended_s3"

  extended_s3_configuration {
    role_arn   = aws_iam_role.firehose_delivery.arn
    bucket_arn = aws_s3_bucket.datalake.arn

    prefix              = "raw/station_status/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/"
    error_output_prefix = "raw/station_status_errors/!{firehose:error-output-type}/year=!{timestamp:yyyy}/month=!{timestamp:MM}/"

    buffering_interval = var.firehose_buffer_interval
    buffering_size     = var.firehose_buffer_size

    compression_format = "UNCOMPRESSED" # Iceberg handles its own Parquet compression

    cloudwatch_logging_options {
      enabled         = true
      log_group_name  = aws_cloudwatch_log_group.firehose_station_status.name
      log_stream_name = aws_cloudwatch_log_stream.firehose_station_status_errors.name
    }
  }

  tags = {
    Name = "${var.project_prefix}-station-status-firehose"
  }
}

##############################################################################
# Kinesis Data Firehose — Weather Observations Stream
##############################################################################

resource "aws_kinesis_firehose_delivery_stream" "weather" {
  name        = "${var.project_prefix}-weather"
  destination = "extended_s3"

  extended_s3_configuration {
    role_arn   = aws_iam_role.firehose_delivery.arn
    bucket_arn = aws_s3_bucket.datalake.arn

    prefix              = "raw/weather/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/"
    error_output_prefix = "raw/weather_errors/!{firehose:error-output-type}/year=!{timestamp:yyyy}/month=!{timestamp:MM}/"

    buffering_interval = var.firehose_buffer_interval
    buffering_size     = var.firehose_buffer_size

    compression_format = "UNCOMPRESSED"

    cloudwatch_logging_options {
      enabled         = true
      log_group_name  = aws_cloudwatch_log_group.firehose_weather.name
      log_stream_name = aws_cloudwatch_log_stream.firehose_weather_errors.name
    }
  }

  tags = {
    Name = "${var.project_prefix}-weather-firehose"
  }
}
