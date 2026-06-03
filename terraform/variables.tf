variable "aws_region" {
  description = "AWS region to deploy all resources."
  type        = string
  default     = "us-east-1"
}

variable "project_prefix" {
  description = "Short prefix used in all resource names (e.g. 'ecobici')."
  type        = string
  default     = "ecobici"
}

variable "s3_bucket_name" {
  description = "Globally unique name for the S3 Data Lake bucket."
  type        = string
  # Example: "ecobici-datalake-123456789012"
}

variable "alert_email" {
  description = "Email address to receive CloudWatch alarm notifications via SNS."
  type        = string
}

variable "athena_results_prefix" {
  description = "S3 prefix where Athena query results are stored."
  type        = string
  default     = "athena-results/"
}

variable "firehose_buffer_interval" {
  description = "Firehose buffer interval in seconds (60–900)."
  type        = number
  default     = 60
}

variable "firehose_buffer_size" {
  description = "Firehose buffer size in MB (1–128)."
  type        = number
  default     = 5
}

variable "lambda_timeout" {
  description = "Default Lambda timeout in seconds."
  type        = number
  default     = 60
}

variable "maintenance_lambda_timeout" {
  description = "Timeout for the Iceberg maintenance Lambda (seconds)."
  type        = number
  default     = 300
}

variable "snapshot_retention_days" {
  description = "Iceberg snapshot retention period in days for expire_snapshots."
  type        = number
  default     = 7
}

variable "tags" {
  description = "Common tags applied to all resources."
  type        = map(string)
  default = {
    Project     = "ecobici-datalake"
    Environment = "production"
    ManagedBy   = "terraform"
  }
}
