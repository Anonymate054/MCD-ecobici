##############################################################################
# Lambda Packaging — zip each Python file at plan/apply time
##############################################################################

data "archive_file" "ingest_gbfs_zip" {
  type        = "zip"
  source_file = "${path.root}/../src/lambdas/ingest_gbfs.py"
  output_path = "${path.root}/../src/lambdas/ingest_gbfs.zip"
}

data "archive_file" "ingest_weather_zip" {
  type        = "zip"
  source_file = "${path.root}/../src/lambdas/ingest_weather.py"
  output_path = "${path.root}/../src/lambdas/ingest_weather.zip"
}

data "archive_file" "maintenance_zip" {
  type        = "zip"
  source_file = "${path.root}/../src/lambdas/maintenance.py"
  output_path = "${path.root}/../src/lambdas/maintenance.zip"
}

##############################################################################
# CloudWatch Log Groups for Lambdas
##############################################################################

resource "aws_cloudwatch_log_group" "lambda_ingest_gbfs" {
  name              = "/aws/lambda/${var.project_prefix}-ingest-gbfs"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "lambda_ingest_weather" {
  name              = "/aws/lambda/${var.project_prefix}-ingest-weather"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "lambda_maintenance" {
  name              = "/aws/lambda/${var.project_prefix}-maintenance"
  retention_in_days = 30
}

##############################################################################
# IAM — Shared Lambda Assume Role Policy
##############################################################################

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

##############################################################################
# IAM — ingest_gbfs Lambda Role & Policy
##############################################################################

resource "aws_iam_role" "lambda_ingest_gbfs" {
  name               = "${var.project_prefix}-lambda-ingest-gbfs-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json

  tags = { Name = "${var.project_prefix}-lambda-ingest-gbfs-role" }
}

data "aws_iam_policy_document" "lambda_ingest_gbfs_policy" {
  # CloudWatch Logs
  statement {
    sid    = "CloudWatchLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.lambda_ingest_gbfs.arn}:*"]
  }

  # Firehose: only station-status stream
  statement {
    sid       = "FirehoseStationStatus"
    effect    = "Allow"
    actions   = ["firehose:PutRecordBatch"]
    resources = [aws_kinesis_firehose_delivery_stream.station_status.arn]
  }

  # No Secrets Manager access needed — GBFS feed is 100% public (no API key)

  # SSM: read/write last_station_info_refresh parameter
  statement {
    sid    = "SSMStationInfoRefresh"
    effect = "Allow"
    actions = [
      "ssm:GetParameter",
      "ssm:PutParameter",
    ]
    resources = [aws_ssm_parameter.last_station_info_refresh.arn]
  }

  # S3: write station_info to data lake
  statement {
    sid       = "S3StationInfoWrite"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.datalake.arn}/raw/station_info/*"]
  }
}

resource "aws_iam_role_policy" "lambda_ingest_gbfs" {
  name   = "ingest-gbfs-policy"
  role   = aws_iam_role.lambda_ingest_gbfs.id
  policy = data.aws_iam_policy_document.lambda_ingest_gbfs_policy.json
}

##############################################################################
# IAM — ingest_weather Lambda Role & Policy
##############################################################################

resource "aws_iam_role" "lambda_ingest_weather" {
  name               = "${var.project_prefix}-lambda-ingest-weather-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json

  tags = { Name = "${var.project_prefix}-lambda-ingest-weather-role" }
}

data "aws_iam_policy_document" "lambda_ingest_weather_policy" {
  statement {
    sid    = "CloudWatchLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.lambda_ingest_weather.arn}:*"]
  }

  statement {
    sid       = "FirehoseWeather"
    effect    = "Allow"
    actions   = ["firehose:PutRecordBatch"]
    resources = [aws_kinesis_firehose_delivery_stream.weather.arn]
  }

  statement {
    sid       = "GetWeatherSecret"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.weather_api.arn]
  }
}

resource "aws_iam_role_policy" "lambda_ingest_weather" {
  name   = "ingest-weather-policy"
  role   = aws_iam_role.lambda_ingest_weather.id
  policy = data.aws_iam_policy_document.lambda_ingest_weather_policy.json
}

##############################################################################
# IAM — maintenance Lambda Role & Policy
##############################################################################

resource "aws_iam_role" "lambda_maintenance" {
  name               = "${var.project_prefix}-lambda-maintenance-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json

  tags = { Name = "${var.project_prefix}-lambda-maintenance-role" }
}

data "aws_iam_policy_document" "lambda_maintenance_policy" {
  statement {
    sid    = "CloudWatchLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.lambda_maintenance.arn}:*"]
  }

  # Athena: submit queries for OPTIMIZE and expire_snapshots
  statement {
    sid    = "AthenaQueryExecution"
    effect = "Allow"
    actions = [
      "athena:StartQueryExecution",
      "athena:GetQueryExecution",
      "athena:GetQueryResults",
      "athena:StopQueryExecution",
    ]
    resources = [
      aws_athena_workgroup.main.arn,
    ]
  }

  # S3: read/write all data lake prefixes (needed for OPTIMIZE rewrites)
  statement {
    sid    = "S3DataLakeReadWrite"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
      "s3:AbortMultipartUpload",
      "s3:GetBucketLocation",
    ]
    resources = [
      aws_s3_bucket.datalake.arn,
      "${aws_s3_bucket.datalake.arn}/*",
    ]
  }

  # Glue: update table metadata after OPTIMIZE
  statement {
    sid    = "GlueCatalogAccess"
    effect = "Allow"
    actions = [
      "glue:GetDatabase",
      "glue:GetTable",
      "glue:GetTableVersions",
      "glue:UpdateTable",
    ]
    resources = [
      "arn:aws:glue:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:catalog",
      "arn:aws:glue:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:database/${aws_glue_catalog_database.ecobici.name}",
      "arn:aws:glue:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${aws_glue_catalog_database.ecobici.name}/*",
    ]
  }
}

resource "aws_iam_role_policy" "lambda_maintenance" {
  name   = "maintenance-policy"
  role   = aws_iam_role.lambda_maintenance.id
  policy = data.aws_iam_policy_document.lambda_maintenance_policy.json
}

##############################################################################
# Lambda Functions
##############################################################################

resource "aws_lambda_function" "ingest_gbfs" {
  function_name = "${var.project_prefix}-ingest-gbfs"
  description   = "Fetches Ecobici GBFS station status (every 5 min) and daily station info."
  role          = aws_iam_role.lambda_ingest_gbfs.arn

  filename         = data.archive_file.ingest_gbfs_zip.output_path
  source_code_hash = data.archive_file.ingest_gbfs_zip.output_base64sha256
  handler          = "ingest_gbfs.handler"
  runtime          = "python3.12"
  timeout          = var.lambda_timeout
  memory_size      = 256

  # No VPC config — avoids NAT Gateway costs

  environment {
    variables = {
      GBFS_DISCOVERY_URL   = "https://gbfs.mex.lyftbikes.com/gbfs/gbfs.json"
      FIREHOSE_STREAM_NAME = aws_kinesis_firehose_delivery_stream.station_status.name
      S3_BUCKET            = aws_s3_bucket.datalake.id
      SSM_REFRESH_PARAM    = aws_ssm_parameter.last_station_info_refresh.name
      GLUE_DATABASE        = aws_glue_catalog_database.ecobici.name
      ATHENA_WORKGROUP     = aws_athena_workgroup.main.name
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda_ingest_gbfs,
  ]

  tags = { Name = "${var.project_prefix}-ingest-gbfs" }
}

resource "aws_lambda_function" "ingest_weather" {
  function_name = "${var.project_prefix}-ingest-weather"
  description   = "Fetches institutional weather observations (every 10 min) with micro-cleaning."
  role          = aws_iam_role.lambda_ingest_weather.arn

  filename         = data.archive_file.ingest_weather_zip.output_path
  source_code_hash = data.archive_file.ingest_weather_zip.output_base64sha256
  handler          = "ingest_weather.handler"
  runtime          = "python3.12"
  timeout          = var.lambda_timeout
  memory_size      = 256

  environment {
    variables = {
      WEATHER_SECRET_NAME  = aws_secretsmanager_secret.weather_api.name
      FIREHOSE_STREAM_NAME = aws_kinesis_firehose_delivery_stream.weather.name
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda_ingest_weather,
  ]

  tags = { Name = "${var.project_prefix}-ingest-weather" }
}

resource "aws_lambda_function" "maintenance" {
  function_name = "${var.project_prefix}-maintenance"
  description   = "Weekly Iceberg OPTIMIZE (BIN_PACK) and expire_snapshots for all tables."
  role          = aws_iam_role.lambda_maintenance.arn

  filename         = data.archive_file.maintenance_zip.output_path
  source_code_hash = data.archive_file.maintenance_zip.output_base64sha256
  handler          = "maintenance.handler"
  runtime          = "python3.12"
  timeout          = var.maintenance_lambda_timeout
  memory_size      = 512

  environment {
    variables = {
      GLUE_DATABASE    = aws_glue_catalog_database.ecobici.name
      ATHENA_WORKGROUP = aws_athena_workgroup.main.name
      S3_BUCKET        = aws_s3_bucket.datalake.id
      SNAPSHOT_DAYS    = tostring(var.snapshot_retention_days)
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda_maintenance,
  ]

  tags = { Name = "${var.project_prefix}-maintenance" }
}

##############################################################################
# EventBridge Rules — Scheduled Triggers
##############################################################################

resource "aws_cloudwatch_event_rule" "ingest_gbfs_schedule" {
  name                = "${var.project_prefix}-ingest-gbfs-schedule"
  description         = "Trigger ingest_gbfs Lambda every 5 minutes."
  schedule_expression = "rate(5 minutes)"
  state               = "ENABLED"

  tags = { Name = "${var.project_prefix}-ingest-gbfs-schedule" }
}

resource "aws_cloudwatch_event_target" "ingest_gbfs_target" {
  rule      = aws_cloudwatch_event_rule.ingest_gbfs_schedule.name
  target_id = "ingest-gbfs-lambda"
  arn       = aws_lambda_function.ingest_gbfs.arn
}

resource "aws_lambda_permission" "eventbridge_invoke_ingest_gbfs" {
  statement_id  = "AllowEventBridgeInvokeGbfs"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingest_gbfs.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.ingest_gbfs_schedule.arn
}

# ---

resource "aws_cloudwatch_event_rule" "ingest_weather_schedule" {
  name                = "${var.project_prefix}-ingest-weather-schedule"
  description         = "Trigger ingest_weather Lambda every 10 minutes."
  schedule_expression = "rate(10 minutes)"
  state               = "ENABLED"

  tags = { Name = "${var.project_prefix}-ingest-weather-schedule" }
}

resource "aws_cloudwatch_event_target" "ingest_weather_target" {
  rule      = aws_cloudwatch_event_rule.ingest_weather_schedule.name
  target_id = "ingest-weather-lambda"
  arn       = aws_lambda_function.ingest_weather.arn
}

resource "aws_lambda_permission" "eventbridge_invoke_ingest_weather" {
  statement_id  = "AllowEventBridgeInvokeWeather"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingest_weather.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.ingest_weather_schedule.arn
}

# ---

resource "aws_cloudwatch_event_rule" "maintenance_schedule" {
  name                = "${var.project_prefix}-maintenance-schedule"
  description         = "Trigger Iceberg maintenance Lambda every Sunday at 02:00 UTC."
  schedule_expression = "cron(0 2 ? * SUN *)"
  state               = "ENABLED"

  tags = { Name = "${var.project_prefix}-maintenance-schedule" }
}

resource "aws_cloudwatch_event_target" "maintenance_target" {
  rule      = aws_cloudwatch_event_rule.maintenance_schedule.name
  target_id = "maintenance-lambda"
  arn       = aws_lambda_function.maintenance.arn
}

resource "aws_lambda_permission" "eventbridge_invoke_maintenance" {
  statement_id  = "AllowEventBridgeInvokeMaintenance"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.maintenance.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.maintenance_schedule.arn
}
