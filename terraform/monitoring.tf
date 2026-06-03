##############################################################################
# SNS Topic — Alert Notifications
##############################################################################

resource "aws_sns_topic" "alerts" {
  name         = "${var.project_prefix}-alerts"
  display_name = "Ecobici Data Lake Alerts"

  tags = { Name = "${var.project_prefix}-alerts" }
}

resource "aws_sns_topic_subscription" "email_alert" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

##############################################################################
# CloudWatch Alarm — ingest_gbfs consecutive failures
# Triggers SNS if the Lambda fails 3 consecutive invocations (15 minutes)
##############################################################################

resource "aws_cloudwatch_metric_alarm" "ingest_gbfs_failures" {
  alarm_name        = "${var.project_prefix}-ingest-gbfs-failures"
  alarm_description = "Fires when ingest_gbfs Lambda fails 3 consecutive times (possible API outage or permissions issue)."
  namespace         = "AWS/Lambda"
  metric_name       = "Errors"
  dimensions = {
    FunctionName = aws_lambda_function.ingest_gbfs.function_name
  }

  statistic           = "Sum"
  period              = 300 # 5-minute evaluation window (matches trigger cadence)
  evaluation_periods  = 3   # 3 consecutive periods = 15 minutes of failures
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = { Name = "${var.project_prefix}-ingest-gbfs-failures-alarm" }
}

##############################################################################
# CloudWatch Alarm — Data Drift Detection
# Uses a custom metric published by a daily Athena query (run via maintenance.py)
# Fires if total bikes_available sums to 0 across the entire network
##############################################################################

resource "aws_cloudwatch_metric_alarm" "data_drift_zero_bikes" {
  alarm_name        = "${var.project_prefix}-data-drift-zero-bikes"
  alarm_description = "Fires when the daily Athena check reports SUM(bikes_available)=0 across the entire Ecobici network. Indicates a data feed failure or Firehose delivery issue."
  namespace         = "EcobiciDataLake"
  metric_name       = "DailyNetworkBikesTotal"
  dimensions = {
    Check = "NetworkAvailability"
  }

  statistic           = "Minimum"
  period              = 86400 # Daily check
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching" # No metric = no data = alarm

  alarm_actions = [aws_sns_topic.alerts.arn]

  tags = { Name = "${var.project_prefix}-data-drift-alarm" }
}

##############################################################################
# CloudWatch Dashboard — Operational Overview
##############################################################################

resource "aws_cloudwatch_dashboard" "ecobici" {
  dashboard_name = "${var.project_prefix}-data-lake"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title   = "Lambda Invocations & Errors"
          view    = "timeSeries"
          stacked = false
          metrics = [
            ["AWS/Lambda", "Invocations", "FunctionName", aws_lambda_function.ingest_gbfs.function_name, { label = "GBFS Invocations" }],
            ["AWS/Lambda", "Errors", "FunctionName", aws_lambda_function.ingest_gbfs.function_name, { label = "GBFS Errors", color = "#d62728" }],
            ["AWS/Lambda", "Invocations", "FunctionName", aws_lambda_function.ingest_weather.function_name, { label = "Weather Invocations" }],
            ["AWS/Lambda", "Errors", "FunctionName", aws_lambda_function.ingest_weather.function_name, { label = "Weather Errors", color = "#ff7f0e" }],
          ]
          period = 300
          region = data.aws_region.current.name
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title = "Lambda Duration (ms)"
          view  = "timeSeries"
          metrics = [
            ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.ingest_gbfs.function_name, { stat = "p99", label = "GBFS p99" }],
            ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.ingest_weather.function_name, { stat = "p99", label = "Weather p99" }],
          ]
          period = 300
          region = data.aws_region.current.name
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          title = "Firehose Records Delivered to S3"
          view  = "timeSeries"
          metrics = [
            ["AWS/Firehose", "DeliveryToS3.Records", "DeliveryStreamName", aws_kinesis_firehose_delivery_stream.station_status.name, { label = "Station Status" }],
            ["AWS/Firehose", "DeliveryToS3.Records", "DeliveryStreamName", aws_kinesis_firehose_delivery_stream.weather.name, { label = "Weather" }],
          ]
          period = 300
          region = data.aws_region.current.name
        }
      },
      {
        type   = "alarm"
        x      = 12
        y      = 6
        width  = 12
        height = 6
        properties = {
          title = "Active Alarms"
          alarms = [
            aws_cloudwatch_metric_alarm.ingest_gbfs_failures.arn,
            aws_cloudwatch_metric_alarm.data_drift_zero_bikes.arn,
          ]
        }
      }
    ]
  })
}
