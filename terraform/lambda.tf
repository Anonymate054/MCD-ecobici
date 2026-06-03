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

data "archive_file" "loader_zip" {
  type        = "zip"
  source_file = "${path.root}/../src/lambdas/loader.py"
  output_path = "${path.root}/../src/lambdas/loader.zip"
}

data "archive_file" "ingest_trips_zip" {
  type        = "zip"
  source_file = "${path.root}/../src/lambdas/ingest_trips.py"
  output_path = "${path.root}/../src/lambdas/ingest_trips.zip"
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

resource "aws_cloudwatch_log_group" "lambda_loader" {
  name              = "/aws/lambda/${var.project_prefix}-loader"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "lambda_ingest_trips" {
  name              = "/aws/lambda/${var.project_prefix}-ingest-trips"
  retention_in_days = 14
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

  # No Secrets Manager access needed — Open-Meteo is 100% public (no API key)

  # S3: read station_info coords
  statement {
    sid       = "S3StationInfoRead"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [
      aws_s3_bucket.datalake.arn,
      "${aws_s3_bucket.datalake.arn}/raw/station_info/*",
    ]
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
      # Open-Meteo is public — no API key needed
      # Station coords are read from today's station_info file in S3
      S3_BUCKET            = aws_s3_bucket.datalake.id
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

##############################################################################
# IAM — loader Lambda Role & Policy
##############################################################################

resource "aws_iam_role" "lambda_loader" {
  name               = "${var.project_prefix}-lambda-loader-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json

  tags = { Name = "${var.project_prefix}-lambda-loader-role" }
}

data "aws_iam_policy_document" "lambda_loader_policy" {
  # CloudWatch Logs
  statement {
    sid    = "CloudWatchLogs"
    effect = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda_loader.arn}:*"]
  }

  # CloudWatch Metrics (custom loader metrics)
  statement {
    sid    = "CloudWatchMetrics"
    effect = "Allow"
    actions = ["cloudwatch:PutMetricData"]
    resources = ["*"]
  }

  # Athena — submit queries and read results
  statement {
    sid    = "AthenaQuery"
    effect = "Allow"
    actions = [
      "athena:StartQueryExecution",
      "athena:GetQueryExecution",
      "athena:GetQueryResults",
      "athena:StopQueryExecution",
    ]
    resources = [
      aws_athena_workgroup.main.arn,
      "arn:aws:athena:${var.aws_region}:${data.aws_caller_identity.current.account_id}:datacatalog/*",
    ]
  }

  # Glue — read/write catalog (tables, databases, partitions)
  statement {
    sid    = "GlueCatalog"
    effect = "Allow"
    actions = [
      "glue:GetDatabase",
      "glue:GetTable",
      "glue:GetTables",
      "glue:CreateTable",
      "glue:DeleteTable",
      "glue:UpdateTable",
      "glue:GetPartition",
      "glue:GetPartitions",
      "glue:BatchCreatePartition",
      "glue:BatchDeletePartition",
    ]
    resources = [
      "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:catalog",
      "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:database/${aws_glue_catalog_database.ecobici.name}",
      "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/${aws_glue_catalog_database.ecobici.name}/*",
    ]
  }

  # S3 — read raw Firehose files, write Athena results, read/write Iceberg data
  statement {
    sid    = "S3DataLake"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = [
      aws_s3_bucket.datalake.arn,
      "${aws_s3_bucket.datalake.arn}/*",
    ]
  }
}

resource "aws_iam_role_policy" "lambda_loader" {
  name   = "loader-policy"
  role   = aws_iam_role.lambda_loader.id
  policy = data.aws_iam_policy_document.lambda_loader_policy.json
}

##############################################################################
# Lambda — loader (Iceberg table loader + rollup, every 30 min)
##############################################################################

resource "aws_lambda_function" "loader" {
  function_name    = "${var.project_prefix}-loader"
  description      = "Incremental Iceberg loader: raw JSON → Iceberg tables + hourly rollup."
  role             = aws_iam_role.lambda_loader.arn
  filename         = data.archive_file.loader_zip.output_path
  source_code_hash = data.archive_file.loader_zip.output_base64sha256
  handler          = "loader.handler"
  runtime          = "python3.12"
  timeout          = 300   # 5 minutes — Athena queries can take up to ~3 min
  memory_size      = 256

  environment {
    variables = {
      GLUE_DATABASE    = aws_glue_catalog_database.ecobici.name
      ATHENA_WORKGROUP = aws_athena_workgroup.main.name
      S3_BUCKET        = aws_s3_bucket.datalake.id
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda_loader,
  ]

  tags = { Name = "${var.project_prefix}-loader" }
}

##############################################################################
# EventBridge — loader schedule (every 30 minutes)
##############################################################################

resource "aws_cloudwatch_event_rule" "loader_schedule" {
  name                = "${var.project_prefix}-loader-schedule"
  description         = "Trigger Iceberg loader Lambda every 30 minutes."
  schedule_expression = "rate(30 minutes)"
  state               = "ENABLED"

  tags = { Name = "${var.project_prefix}-loader-schedule" }
}

resource "aws_cloudwatch_event_target" "loader_target" {
  rule      = aws_cloudwatch_event_rule.loader_schedule.name
  target_id = "loader-lambda"
  arn       = aws_lambda_function.loader.arn
}

resource "aws_lambda_permission" "eventbridge_invoke_loader" {
  statement_id  = "AllowEventBridgeInvokeLoader"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.loader.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.loader_schedule.arn
}

##############################################################################
# IAM — ingest_trips Lambda Role & Policy
##############################################################################

resource "aws_iam_role" "lambda_ingest_trips" {
  name               = "${var.project_prefix}-lambda-ingest-trips-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json

  tags = { Name = "${var.project_prefix}-lambda-ingest-trips-role" }
}

data "aws_iam_policy_document" "lambda_ingest_trips_policy" {
  # CloudWatch Logs
  statement {
    sid    = "CloudWatchLogs"
    effect = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda_ingest_trips.arn}:*"]
  }

  # Athena — submit queries and read results
  statement {
    sid    = "AthenaQuery"
    effect = "Allow"
    actions = [
      "athena:StartQueryExecution",
      "athena:GetQueryExecution",
      "athena:GetQueryResults",
      "athena:StopQueryExecution",
    ]
    resources = [
      aws_athena_workgroup.main.arn,
      "arn:aws:athena:${var.aws_region}:${data.aws_caller_identity.current.account_id}:datacatalog/*",
    ]
  }

  # Glue — read/write catalog (tables, databases, partitions)
  statement {
    sid    = "GlueCatalog"
    effect = "Allow"
    actions = [
      "glue:GetDatabase",
      "glue:GetTable",
      "glue:GetTables",
      "glue:CreateTable",
      "glue:DeleteTable",
      "glue:UpdateTable",
      "glue:GetPartition",
      "glue:GetPartitions",
      "glue:BatchCreatePartition",
      "glue:BatchDeletePartition",
    ]
    resources = [
      "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:catalog",
      "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:database/${aws_glue_catalog_database.ecobici.name}",
      "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/${aws_glue_catalog_database.ecobici.name}/*",
    ]
  }

  # S3 — read raw CSV files, write Athena results, read/write Iceberg data
  statement {
    sid    = "S3DataLake"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = [
      aws_s3_bucket.datalake.arn,
      "${aws_s3_bucket.datalake.arn}/*",
    ]
  }
}

resource "aws_iam_role_policy" "lambda_ingest_trips" {
  name   = "ingest-trips-policy"
  role   = aws_iam_role.lambda_ingest_trips.id
  policy = data.aws_iam_policy_document.lambda_ingest_trips_policy.json
}

##############################################################################
# Lambda — ingest_trips (automated monthly CSV ingestion)
##############################################################################

resource "aws_lambda_function" "ingest_trips" {
  function_name    = "${var.project_prefix}-ingest-trips"
  description      = "Ingest historical/incremental trips from portal into Iceberg partitioned table."
  role             = aws_iam_role.lambda_ingest_trips.arn
  filename         = data.archive_file.ingest_trips_zip.output_path
  source_code_hash = data.archive_file.ingest_trips_zip.output_base64sha256
  handler          = "ingest_trips.handler"
  runtime          = "python3.12"
  timeout          = 900   # 15 minutes — download, upload and INSERT can take several minutes
  memory_size      = 512   # plenty of memory for basic stream forwarding

  environment {
    variables = {
      GLUE_DATABASE    = aws_glue_catalog_database.ecobici.name
      ATHENA_WORKGROUP = aws_athena_workgroup.main.name
      S3_BUCKET        = aws_s3_bucket.datalake.id
      START_MONTH      = "2023-01"
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda_ingest_trips,
  ]

  tags = { Name = "${var.project_prefix}-ingest-trips" }
}

##############################################################################
# EventBridge — ingest_trips schedule (every day at 06:00 UTC)
##############################################################################

resource "aws_cloudwatch_event_rule" "ingest_trips_schedule" {
  name                = "${var.project_prefix}-ingest-trips-schedule"
  description         = "Trigger Ecobici trips ingestion daily to check for new published months."
  schedule_expression = "cron(0 6 * * ? *)"
  state               = "ENABLED"

  tags = { Name = "${var.project_prefix}-ingest-trips-schedule" }
}

resource "aws_cloudwatch_event_target" "ingest_trips_target" {
  rule      = aws_cloudwatch_event_rule.ingest_trips_schedule.name
  target_id = "ingest-trips-lambda"
  arn       = aws_lambda_function.ingest_trips.arn
}

resource "aws_lambda_permission" "eventbridge_invoke_ingest_trips" {
  statement_id  = "AllowEventBridgeInvokeIngestTrips"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingest_trips.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.ingest_trips_schedule.arn
}
