# Semantic Layer — Ecobici Datalake Data Dictionary

This document contains descriptions, column schemas, and representative samples for each table available in the Ecobici Datalake (`ecobici_lake`).

## Tabla: `raw_station_status`
**Descripción:** 5-minute raw bike and dock availability snapshots ingested directly from the Ecobici GBFS feed.

### Column Structure
| Columna | Description |
| --- | --- |
| `timestamp` | Timestamp of the observation (UTC). |
| `station_id` | Unique identifier of the station. |
| `bikes_available` | Number of functional bikes currently at the station. |
| `docks_available` | Number of free docks currently at the station. |
| `is_renting` | Boolean flag indicating if renting bikes is enabled. |
| `is_returning` | Boolean flag indicating if returning bikes is enabled. |
| `_ingest_at` | Timestamp when the record was ingested by the Lambda function. |
| `station_state` | Operational state heuristic at the moment of ingestion (NORMAL, STARVED, OVERFLOW). |

### Data Sample
| timestamp | station_id | bikes_available | docks_available | is_renting | is_returning | _ingest_at | station_state |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-06-09 21:31:50.000000 | 1 | 10 | 25 | true | true | 2026-06-09 21:31:50.000000 |  |
| 2026-06-09 21:31:50.000000 | 5 | 0 | 19 | true | true | 2026-06-09 21:31:50.000000 |  |
| 2026-06-09 21:31:50.000000 | 6 | 3 | 22 | true | true | 2026-06-09 21:31:50.000000 |  |

---

## Tabla: `weather_observations`
**Descripción:** Real-time weather observations ingested every 15 minutes from Open-Meteo current endpoint.

### Column Structure
| Columna | Description |
| --- | --- |
| `timestamp` | Timestamp of the weather observation (UTC). |
| `station_id` | Unique identifier of the nearest Ecobici station. |
| `temp_c` | Temperature in Celsius. |
| `precip_mm` | Precipitation in millimeters. |
| `_is_filled` | Boolean indicating if the observation was backfilled/imputed. |

### Data Sample
| timestamp | station_id | temp_c | precip_mm | _is_filled |
| --- | --- | --- | --- | --- |
| 2026-06-09 08:07:07.000000 | 1 | 15.5 | 0.1 | false |
| 2026-06-09 08:07:07.000000 | 5 | 15.4 | 0.1 | false |
| 2026-06-09 08:07:07.000000 | 6 | 15.4 | 0.1 | false |

---

## Tabla: `station_status_15m`
**Descripción:** Online 15-minute intermediate aggregated rollup combining station statuses and real-time weather.

### Column Structure
| Columna | Description |
| --- | --- |
| `timestamp` | Start of the 15-minute time window (UTC). |
| `station_id` | Unique identifier of the station. |
| `avg_bikes_available` | Average number of bikes available during the 15-minute slot. |
| `avg_docks_available` | Average number of docks available during the 15-minute slot. |
| `total_renting_minutes` | Approximate duration (minutes) the station was renting bikes in this slot. |
| `total_returning_minutes` | Approximate duration (minutes) the station was accepting returns in this slot. |
| `temp_c` | Average temperature in Celsius during this time slot. |
| `precip_mm` | Average precipitation in millimeters during this time slot. |
| `station_state` | Aggregated operational state heuristic (NORMAL, STARVED, OVERFLOW, REBALANCED_REFILL, REBALANCED_DEPLETE). |

### Data Sample
| timestamp | station_id | avg_bikes_available | avg_docks_available | total_renting_minutes | total_returning_minutes | temp_c | precip_mm | station_state |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-06-10 03:00:00.000000 | 1 | 1.0 | 35.0 | 15 | 15 | 16.25 | 0.1 | NORMAL |
| 2026-06-10 03:00:00.000000 | 10 | 0.0 | 22.0 | 15 | 15 | 16.85 | 0.0 | STARVED |
| 2026-06-10 03:00:00.000000 | 100 | 0.0 | 23.0 | 15 | 15 | 16.35 | 0.6000000000000001 | STARVED |

---

## Tabla: `hourly_station_status`
**Descripción:** Online 1-hour primary aggregated rollup table containing weather, state, and heuristic broken flags.

### Column Structure
| Columna | Description |
| --- | --- |
| `hour` | Start of the 1-hour time window (UTC). |
| `station_id` | Unique identifier of the station. |
| `avg_bikes_available` | Average number of bikes available during the hour. |
| `avg_docks_available` | Average number of docks available during the hour. |
| `total_renting_minutes` | Total active renting duration during the hour. |
| `total_returning_minutes` | Total active returning duration during the hour. |
| `is_heuristically_broken` | Boolean flag indicating if a station had 0 activity or constant status suggesting connectivity/sensor failure. |
| `temp_c` | Average temperature in Celsius during the hour. |
| `precip_mm` | Average precipitation in millimeters during the hour. |
| `station_state` | Operational state classifier (NORMAL, STARVED, OVERFLOW, REBALANCED_REFILL, REBALANCED_DEPLETE, BROKEN). |

### Data Sample
| hour | station_id | avg_bikes_available | avg_docks_available | total_renting_minutes | total_returning_minutes | is_heuristically_broken | temp_c | precip_mm | station_state |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-06-10 03:00:00.000000 | 1 | 0.5 | 35.5 | 30 | 30 | false | 16.25 | 0.1 | STARVED |
| 2026-06-10 03:00:00.000000 | 10 | 0.0 | 22.0 | 30 | 30 | false | 16.85 | 0.0 | STARVED |
| 2026-06-10 03:00:00.000000 | 100 | 0.0 | 23.0 | 30 | 30 | false | 16.35 | 0.6000000000000001 | STARVED |

---

## Tabla: `ecobici_station_info`
**Descripción:** Static physical metadata for all Ecobici stations (locations, capacities, names).

### Column Structure
| Columna | Description |
| --- | --- |
| `station_id` | Unique identifier of the station. |
| `name` | Street name/location identifier. |
| `capacity` | Total physical docks capacity of the station. |
| `lat` | Latitude coordinate. |
| `lon` | Longitude coordinate. |
| `_updated_at` | Timestamp of the last metadata update. |

### Data Sample
| station_id | name | lat | lon | capacity | _updated_at |
| --- | --- | --- | --- | --- | --- |
| 1 | CE-710 Molino del Rey - Glorieta de la Lealtad | 19.416795 | -99.192508 | 39 | 2026-06-03 05:41:38.000000 |
| 12 | CE-417 Goya-Augusto Rodin | 19.373447 | -99.184465 | 27 | 2026-06-03 05:41:38.000000 |
| 14 | CE-022 Reforma - Manchester | 19.424784 | -99.172119 | 35 | 2026-06-03 05:41:38.000000 |

---

## Tabla: `trips`
**Descripción:** Historical individual trip records from Ecobici, containing trip start/end times and stations.

### Column Structure
| Columna | Description |
| --- | --- |
| `trip_id` | Unique identifier of the trip. |
| `user_gender` | Gender of the user. |
| `user_age` | Age of the user. |
| `start_timestamp` | Start timestamp of the trip (UTC). |
| `start_station_id` | ID of the station where the trip started. |
| `end_timestamp` | End timestamp of the trip (UTC). |
| `end_station_id` | ID of the station where the trip ended. |

### Data Sample
| user_gender | user_age | bike_id | start_station_id | end_station_id | start_timestamp | end_timestamp |
| --- | --- | --- | --- | --- | --- | --- |
| F | 28 | 3556412 | 136 | 139 | 2026-04-30 23:26:51.000000 | 2026-05-01 00:00:01.000000 |
| M | 36 | 6736532 | 77 | 150 | 2026-04-30 23:51:48.000000 | 2026-05-01 00:00:04.000000 |
| M | 30 | 2483857 | 130 | 133 | 2026-04-30 23:57:31.000000 | 2026-05-01 00:00:04.000000 |

---

## Tabla: `historical_station_status_15m`
**Descripción:** Simulated historical 15-minute station status reconstructed using bounded flow simulation on trip records.

### Column Structure
| Columna | Description |
| --- | --- |
| `timestamp` | 15-minute time window start (UTC). |
| `station_id` | Unique identifier of the station. |
| `checkouts` | Number of trip checkouts during this 15-minute interval. |
| `checkins` | Number of trip checkins during this 15-minute interval. |
| `net_delta` | Net flow change of bikes (checkins - checkouts). |
| `estimated_bikes_available` | Estimated number of bikes available at the station. |
| `estimated_docks_available` | Estimated number of docks available at the station. |
| `capacity` | Physical capacity of the station. |
| `station_state` | Simulated operational state heuristic (NORMAL, STARVED, OVERFLOW, REBALANCED_REFILL, REBALANCED_DEPLETE). |

### Data Sample
| timestamp | station_id | checkouts | checkins | net_delta | estimated_bikes_available | estimated_docks_available | capacity | station_state |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-05-01 00:00:00.000000 | 5 | 0 | 1 | 1 | 10.0 | 9.0 | 19 | NORMAL |
| 2026-05-01 00:15:00.000000 | 5 | 0 | 0 | 0 | 10.0 | 9.0 | 19 | NORMAL |
| 2026-05-01 00:30:00.000000 | 5 | 0 | 0 | 0 | 10.0 | 9.0 | 19 | NORMAL |

---

## Tabla: `historical_station_status_1h`
**Descripción:** Simulated historical 1-hour station status, aggregated from the 15-minute simulation and joined with weather data.

### Column Structure
| Columna | Description |
| --- | --- |
| `hour` | Start of the 1-hour window (UTC). |
| `station_id` | Unique identifier of the station. |
| `checkouts` | Total trip checkouts during the hour. |
| `checkins` | Total trip checkins during the hour. |
| `net_delta` | Net flow change of bikes (checkins - checkouts). |
| `estimated_bikes_available` | Estimated average number of bikes available. |
| `estimated_docks_available` | Estimated average number of docks available. |
| `capacity` | Physical capacity of the station. |
| `temp_c` | Imputed temperature in Celsius. |
| `precip_mm` | Imputed precipitation in millimeters. |
| `station_state` | Aggregated simulated operational state classifier (NORMAL, STARVED, OVERFLOW, REBALANCED_REFILL, REBALANCED_DEPLETE). |

### Data Sample
| hour | station_id | checkouts | checkins | net_delta | estimated_bikes_available | estimated_docks_available | capacity | temp_c | precip_mm | station_state |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-05-01 00:00:00.000000 | 11 | 0 | 2 | 2 | 14.0 | 13.0 | 27 |  | 0.0 | NORMAL |
| 2026-05-01 01:00:00.000000 | 11 | 0 | 0 | 0 | 14.0 | 13.0 | 27 |  | 0.0 | NORMAL |
| 2026-05-01 02:00:00.000000 | 11 | 0 | 0 | 0 | 14.0 | 13.0 | 27 |  | 0.0 | NORMAL |

---
