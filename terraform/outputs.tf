output "s3_datalake_bucket" {
  description = "Name of the S3 Data Lake bucket."
  value       = aws_s3_bucket.datalake.id
}

output "s3_datalake_arn" {
  description = "ARN of the S3 Data Lake bucket."
  value       = aws_s3_bucket.datalake.arn
}

output "glue_database_name" {
  description = "Glue catalog database name."
  value       = aws_glue_catalog_database.ecobici.name
}

output "athena_workgroup" {
  description = "Athena workgroup name."
  value       = aws_athena_workgroup.main.name
}

output "firehose_station_status_arn" {
  description = "ARN of the station-status Kinesis Firehose stream."
  value       = aws_kinesis_firehose_delivery_stream.station_status.arn
}

output "firehose_weather_arn" {
  description = "ARN of the weather Kinesis Firehose stream."
  value       = aws_kinesis_firehose_delivery_stream.weather.arn
}

output "lambda_ingest_gbfs_arn" {
  description = "ARN of the ingest_gbfs Lambda function."
  value       = aws_lambda_function.ingest_gbfs.arn
}

output "lambda_ingest_weather_arn" {
  description = "ARN of the ingest_weather Lambda function."
  value       = aws_lambda_function.ingest_weather.arn
}

output "lambda_maintenance_arn" {
  description = "ARN of the maintenance Lambda function."
  value       = aws_lambda_function.maintenance.arn
}

output "lambda_loader_arn" {
  description = "ARN of the Iceberg loader Lambda (runs every 30 min)."
  value       = aws_lambda_function.loader.arn
}

output "sns_alerts_arn" {
  description = "ARN of the SNS alerts topic."
  value       = aws_sns_topic.alerts.arn
}

output "gbfs_discovery_url" {
  description = "Public GBFS discovery URL (no authentication required)."
  value       = "https://gbfs.mex.lyftbikes.com/gbfs/gbfs.json"
}

output "open_meteo_url" {
  description = "Public weather API (Open-Meteo) — no authentication required."
  value       = "https://api.open-meteo.com/v1/forecast"
}

output "ssm_station_info_refresh_param" {
  description = "SSM parameter name tracking daily station_info refresh."
  value       = aws_ssm_parameter.last_station_info_refresh.name
}

output "cloudwatch_dashboard_url" {
  description = "URL to the CloudWatch operational dashboard."
  value       = "https://${data.aws_region.current.name}.console.aws.amazon.com/cloudwatch/home?region=${data.aws_region.current.name}#dashboards:name=${aws_cloudwatch_dashboard.ecobici.dashboard_name}"
}
