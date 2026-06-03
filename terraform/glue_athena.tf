##############################################################################
# Glue Data Catalog — Database
##############################################################################

resource "aws_glue_catalog_database" "ecobici" {
  name        = "${var.project_prefix}_lake"
  description = "Glue catalog database for the Ecobici CDMX Data Lake (Iceberg tables)."
}

##############################################################################
# Iceberg Tables via Glue
# Notes:
#   - table_type = "ICEBERG" requires Glue Catalog with Iceberg support.
#   - Athena engine v3 is used for all DML (see workgroup in main.tf).
#   - Partitioning is declared via parameters; Iceberg manages partition evolution.
##############################################################################

# --- 1. raw_station_status ---------------------------------------------------

resource "aws_glue_catalog_table" "raw_station_status" {
  name          = "raw_station_status"
  database_name = aws_glue_catalog_database.ecobici.name
  description   = "5-minute bike station availability snapshots from the Ecobici GBFS feed."

  open_table_format_input {
    iceberg_input {
      metadata_operation = "CREATE"
      version            = "2"
    }
  }

  storage_descriptor {
    location      = "s3://${var.s3_bucket_name}/raw/station_status/"
    input_format  = "org.apache.iceberg.mr.mapred.IcebergInputFormat"
    output_format = "org.apache.iceberg.mr.mapred.IcebergOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.iceberg.mr.mapred.IcebergOutputFormat"
    }

    columns {
      name    = "timestamp"
      type    = "timestamp"
      comment = "Ingestion time (UTC)"
    }
    columns {
      name    = "station_id"
      type    = "string"
      comment = "Ecobici station identifier"
    }
    columns {
      name    = "bikes_available"
      type    = "int"
      comment = "Number of available bikes"
    }
    columns {
      name    = "docks_available"
      type    = "int"
      comment = "Number of available docking slots"
    }
    columns {
      name    = "is_renting"
      type    = "boolean"
      comment = "True if station is accepting bike pickups"
    }
    columns {
      name    = "is_returning"
      type    = "boolean"
      comment = "True if station is accepting bike returns"
    }
    columns {
      name    = "_ingest_at"
      type    = "timestamp"
      comment = "Lambda processing timestamp"
    }
  }

  parameters = {
    "table_type"        = "ICEBERG"
    "format"            = "parquet"
    "write_compression" = "snappy"
    "partition_spec"    = "[{\"name\":\"day\",\"transform\":\"day\",\"source-id\":1}]"
  }
}

# --- 2. ecobici_station_info -------------------------------------------------

resource "aws_glue_catalog_table" "ecobici_station_info" {
  name          = "ecobici_station_info"
  database_name = aws_glue_catalog_database.ecobici.name
  description   = "Daily refreshed Ecobici station metadata (SCD Type 1)."

  open_table_format_input {
    iceberg_input {
      metadata_operation = "CREATE"
      version            = "2"
    }
  }

  storage_descriptor {
    location      = "s3://${var.s3_bucket_name}/raw/station_info/"
    input_format  = "org.apache.iceberg.mr.mapred.IcebergInputFormat"
    output_format = "org.apache.iceberg.mr.mapred.IcebergOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.iceberg.mr.mapred.IcebergOutputFormat"
    }

    columns {
      name    = "station_id"
      type    = "string"
      comment = "Unique station identifier"
    }
    columns {
      name    = "name"
      type    = "string"
      comment = "Human-readable station name"
    }
    columns {
      name    = "lat"
      type    = "double"
      comment = "Latitude (WGS84)"
    }
    columns {
      name    = "lon"
      type    = "double"
      comment = "Longitude (WGS84)"
    }
    columns {
      name    = "capacity"
      type    = "int"
      comment = "Total number of docking points"
    }
    columns {
      name    = "_updated_at"
      type    = "timestamp"
      comment = "Timestamp of last info refresh"
    }
  }

  parameters = {
    "table_type"        = "ICEBERG"
    "format"            = "parquet"
    "write_compression" = "snappy"
  }
}

# --- 3. weather_observations -------------------------------------------------

resource "aws_glue_catalog_table" "weather_observations" {
  name          = "weather_observations"
  database_name = aws_glue_catalog_database.ecobici.name
  description   = "10-minute institutional weather observations (SMN/REDMET/OH-UNAM), micro-cleaned."

  open_table_format_input {
    iceberg_input {
      metadata_operation = "CREATE"
      version            = "2"
    }
  }

  storage_descriptor {
    location      = "s3://${var.s3_bucket_name}/raw/weather/"
    input_format  = "org.apache.iceberg.mr.mapred.IcebergInputFormat"
    output_format = "org.apache.iceberg.mr.mapred.IcebergOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.iceberg.mr.mapred.IcebergOutputFormat"
    }

    columns {
      name    = "timestamp"
      type    = "timestamp"
      comment = "Observation time (UTC)"
    }
    columns {
      name    = "station_id"
      type    = "string"
      comment = "Weather station identifier"
    }
    columns {
      name    = "temp_c"
      type    = "double"
      comment = "Temperature in Celsius (validated: -10 to 50)"
    }
    columns {
      name    = "precip_mm"
      type    = "double"
      comment = "Precipitation in millimeters"
    }
    columns {
      name    = "_is_filled"
      type    = "boolean"
      comment = "True if value was forward-filled (missing up to 30 min)"
    }
  }

  parameters = {
    "table_type"        = "ICEBERG"
    "format"            = "parquet"
    "write_compression" = "snappy"
    "partition_spec"    = "[{\"name\":\"day\",\"transform\":\"day\",\"source-id\":1}]"
  }
}

# --- 4. hourly_station_status ------------------------------------------------

resource "aws_glue_catalog_table" "hourly_station_status" {
  name          = "hourly_station_status"
  database_name = aws_glue_catalog_database.ecobici.name
  description   = "Hourly rollup with heuristic malfunction detection flag. Populated via Athena CTAS."

  open_table_format_input {
    iceberg_input {
      metadata_operation = "CREATE"
      version            = "2"
    }
  }

  storage_descriptor {
    location      = "s3://${var.s3_bucket_name}/processed/hourly_station_status/"
    input_format  = "org.apache.iceberg.mr.mapred.IcebergInputFormat"
    output_format = "org.apache.iceberg.mr.mapred.IcebergOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.iceberg.mr.mapred.IcebergOutputFormat"
    }

    columns {
      name    = "hour"
      type    = "timestamp"
      comment = "Truncated to hour (UTC)"
    }
    columns {
      name    = "station_id"
      type    = "string"
      comment = "Ecobici station identifier"
    }
    columns {
      name    = "avg_bikes_available"
      type    = "double"
      comment = "Average bikes available during the hour"
    }
    columns {
      name    = "avg_docks_available"
      type    = "double"
      comment = "Average docks available during the hour"
    }
    columns {
      name    = "total_renting_minutes"
      type    = "int"
      comment = "Minutes in the hour where is_renting=true"
    }
    columns {
      name    = "total_returning_minutes"
      type    = "int"
      comment = "Minutes in the hour where is_returning=true"
    }
    columns {
      name    = "is_heuristically_broken"
      type    = "boolean"
      comment = "True if heuristic malfunction detected (frozen count or native flag)"
    }
    columns {
      name    = "temp_c"
      type    = "double"
      comment = "Nearest weather station temperature (joined via vw_ecobici_weather_mapping)"
    }
    columns {
      name    = "precip_mm"
      type    = "double"
      comment = "Nearest weather station precipitation"
    }
  }

  parameters = {
    "table_type"        = "ICEBERG"
    "format"            = "parquet"
    "write_compression" = "snappy"
    "partition_spec"    = "[{\"name\":\"month\",\"transform\":\"month\",\"source-id\":1}]"
  }
}
