# Capa Semántica — Diccionario de Datos Datalake Ecobici

Este documento contiene la descripción, los campos y una muestra representativa de cada una de las tablas disponibles en el Datalake de Ecobici (`ecobici_lake`).

## Tabla: `raw_station_status`
**Descripción:** Muestras crudas de disponibilidad de bicis y puertos cada 5 minutos obtenidas directamente de la API GBFS de Ecobici.

### Estructura de Columnas
| Columna | Significado / Descripción |
| --- | --- |
| `timestamp` | Marca de tiempo de la observación (UTC). |
| `station_id` | Identificador único de la estación. |
| `bikes_available` | Número de bicicletas funcionales disponibles en la estación. |
| `docks_available` | Número de puertos libres disponibles en la estación. |
| `is_renting` | Indica si la renta de bicicletas está activa. |
| `is_returning` | Indica si el retorno de bicicletas está activo. |
| `_ingest_at` | Marca de tiempo en la que la función Lambda procesó el registro. |
| `station_state` | Heurística del estado operativo en el momento de la ingesta (NORMAL, STARVED, OVERFLOW). |

### Muestra de Datos (Sample)
| timestamp | station_id | bikes_available | docks_available | is_renting | is_returning | _ingest_at | station_state |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-06-09 18:41:50.000000 | 1 | 8 | 28 | true | true | 2026-06-09 18:41:50.000000 |  |
| 2026-06-09 18:41:50.000000 | 5 | 4 | 15 | true | true | 2026-06-09 18:41:50.000000 |  |
| 2026-06-09 18:41:50.000000 | 6 | 3 | 22 | true | true | 2026-06-09 18:41:50.000000 |  |

---

## Tabla: `weather_observations`
**Descripción:** Observaciones climáticas en tiempo real ingeridas cada 15 minutos desde el endpoint 'current' de Open-Meteo.

### Estructura de Columnas
| Columna | Significado / Descripción |
| --- | --- |
| `timestamp` | Marca de tiempo de la observación climática (UTC). |
| `station_id` | Identificador de la estación de Ecobici más cercana a las coordenadas. |
| `temp_c` | Temperatura en grados Celsius. |
| `precip_mm` | Precipitación en milímetros. |
| `_is_filled` | Indica si el registro es imputado o de respaldo. |

### Muestra de Datos (Sample)
| timestamp | station_id | temp_c | precip_mm | _is_filled |
| --- | --- | --- | --- | --- |
| 2026-06-08 13:07:07.000000 | 1 | 15.2 | 0.0 | false |
| 2026-06-08 13:07:07.000000 | 5 | 15.5 | 0.0 | false |
| 2026-06-08 13:07:07.000000 | 6 | 15.5 | 0.0 | false |

---

## Tabla: `station_status_15m`
**Descripción:** Agregaciones en ventanas de 15 minutos combinando estado de la estación y clima en tiempo real.

### Estructura de Columnas
| Columna | Significado / Descripción |
| --- | --- |
| `timestamp` | Inicio de la ventana de 15 minutos (UTC). |
| `station_id` | Identificador único de la estación. |
| `avg_bikes_available` | Promedio de bicicletas disponibles durante la ventana de 15 minutos. |
| `avg_docks_available` | Promedio de puertos disponibles durante la ventana de 15 minutos. |
| `total_renting_minutes` | Minutos aproximados que la estación estuvo activa para renta. |
| `total_returning_minutes` | Minutos aproximados que la estación estuvo activa para retornos. |
| `temp_c` | Temperatura promedio en Celsius durante el intervalo. |
| `precip_mm` | Precipitación promedio en milímetros durante el intervalo. |
| `station_state` | Estado operativo consolidado (NORMAL, STARVED, OVERFLOW, REBALANCED_REFILL, REBALANCED_DEPLETE). |

### Muestra de Datos (Sample)
| timestamp | station_id | avg_bikes_available | avg_docks_available | total_renting_minutes | total_returning_minutes | temp_c | precip_mm | station_state |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-06-03 10:30:00.000000 | 510 | 0.0 | 22.0 | 0 | 15 | 15.15 | 0.0 | STARVED |
| 2026-06-03 10:30:00.000000 | 511 | 0.0 | 10.0 | 0 | 15 | 15.15 | 0.0 | STARVED |
| 2026-06-03 10:30:00.000000 | 512 | 0.0 | 15.0 | 0 | 15 | 15.15 | 0.0 | STARVED |

---

## Tabla: `hourly_station_status`
**Descripción:** Agregación por hora para analítica que incluye variables climatológicas y banderas de error.

### Estructura de Columnas
| Columna | Significado / Descripción |
| --- | --- |
| `hour` | Inicio de la hora agregada (UTC). |
| `station_id` | Identificador único de la estación. |
| `avg_bikes_available` | Promedio de bicicletas disponibles durante la hora. |
| `avg_docks_available` | Promedio de puertos disponibles durante la hora. |
| `total_renting_minutes` | Duración total (minutos) de renta activa durante la hora. |
| `total_returning_minutes` | Duración total (minutos) de retorno activo durante la hora. |
| `is_heuristically_broken` | Banderas que identifica estaciones sin cambios en el estado (posible falla de red/sensor). |
| `temp_c` | Temperatura promedio en Celsius durante la hora. |
| `precip_mm` | Precipitación promedio en milímetros durante la hora. |
| `station_state` | Estado operativo consolidado final (NORMAL, STARVED, OVERFLOW, REBALANCED_REFILL, REBALANCED_DEPLETE, BROKEN). |

### Muestra de Datos (Sample)
| hour | station_id | avg_bikes_available | avg_docks_available | total_renting_minutes | total_returning_minutes | is_heuristically_broken | temp_c | precip_mm | station_state |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-06-10 03:00:00.000000 | 1 | 0.5 | 35.5 | 30 | 30 | false | 16.25 | 0.1 | STARVED |
| 2026-06-10 03:00:00.000000 | 10 | 0.0 | 22.0 | 30 | 30 | false | 16.85 | 0.0 | STARVED |
| 2026-06-10 03:00:00.000000 | 100 | 0.0 | 23.0 | 30 | 30 | false | 16.35 | 0.6000000000000001 | STARVED |

---

## Tabla: `ecobici_station_info`
**Descripción:** Catálogo físico y metadatos estáticos de las estaciones de Ecobici.

### Estructura de Columnas
| Columna | Significado / Descripción |
| --- | --- |
| `station_id` | Identificador único de la estación. |
| `name` | Nombre físico de la estación (calles/ubicación). |
| `capacity` | Capacidad total física de puertos (docks). |
| `lat` | Coordenada de latitud. |
| `lon` | Coordenada de longitud. |
| `_updated_at` | Última fecha de actualización del catálogo. |

### Muestra de Datos (Sample)
| station_id | name | lat | lon | capacity | _updated_at |
| --- | --- | --- | --- | --- | --- |
| 1 | CE-710 Molino del Rey - Glorieta de la Lealtad | 19.416795 | -99.192508 | 39 | 2026-06-03 05:41:38.000000 |
| 12 | CE-417 Goya-Augusto Rodin | 19.373447 | -99.184465 | 27 | 2026-06-03 05:41:38.000000 |
| 14 | CE-022 Reforma - Manchester | 19.424784 | -99.172119 | 35 | 2026-06-03 05:41:38.000000 |

---

## Tabla: `trips`
**Descripción:** Historial de viajes individuales de Ecobici utilizado para simulaciones y modelado predictivo.

### Estructura de Columnas
| Columna | Significado / Descripción |
| --- | --- |
| `trip_id` | ID único del viaje. |
| `user_gender` | Género del usuario. |
| `user_age` | Edad del usuario. |
| `start_timestamp` | Marca de tiempo de inicio del viaje (UTC). |
| `start_station_id` | ID de la estación de origen. |
| `end_timestamp` | Marca de tiempo de término del viaje (UTC). |
| `end_station_id` | ID de la estación de destino. |

### Muestra de Datos (Sample)
| user_gender | user_age | bike_id | start_station_id | end_station_id | start_timestamp | end_timestamp |
| --- | --- | --- | --- | --- | --- | --- |
| F | 24 | 8293276 | 126 | 547 | 2024-01-31 23:34:37.000000 | 2024-02-01 00:00:03.000000 |
| M | 27 | 5494331 | 499 | 495 | 2024-01-31 23:55:28.000000 | 2024-02-01 00:00:04.000000 |
| M | 33 | 2714178 | 116 | 495 | 2024-01-31 23:41:59.000000 | 2024-02-01 00:00:08.000000 |

---

## Tabla: `historical_station_status_15m`
**Descripción:** Disponibilidad histórica agregada cada 15 minutos reconstruida mediante simulación de flujos de viajes.

### Estructura de Columnas
| Columna | Significado / Descripción |
| --- | --- |
| `timestamp` | Inicio de la ventana de 15 minutos simulada (UTC). |
| `station_id` | Identificador único de la estación. |
| `checkouts` | Viajes iniciados (salidas) en el intervalo. |
| `checkins` | Viajes finalizados (llegadas) en el intervalo. |
| `net_delta` | Diferencia neta de flujo (entradas - salidas). |
| `estimated_bikes_available` | Estimación del total de bicicletas disponibles. |
| `estimated_docks_available` | Estimación del total de puertos libres disponibles. |
| `capacity` | Capacidad total de la estación. |
| `station_state` | Estado operativo simulado (NORMAL, STARVED, OVERFLOW, REBALANCED_REFILL, REBALANCED_DEPLETE). |

### Muestra de Datos (Sample)
| timestamp | station_id | checkouts | checkins | net_delta | estimated_bikes_available | estimated_docks_available | capacity | station_state |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-05-01 00:00:00.000000 | 5 | 0 | 1 | 1 | 10.0 | 9.0 | 19 | NORMAL |
| 2026-05-01 00:15:00.000000 | 5 | 0 | 0 | 0 | 10.0 | 9.0 | 19 | NORMAL |
| 2026-05-01 00:30:00.000000 | 5 | 0 | 0 | 0 | 10.0 | 9.0 | 19 | NORMAL |

---

## Tabla: `historical_station_status_1h`
**Descripción:** Disponibilidad histórica simulada agregada por hora y combinada con el histórico de clima.

### Estructura de Columnas
| Columna | Significado / Descripción |
| --- | --- |
| `hour` | Inicio de la hora simulada (UTC). |
| `station_id` | Identificador único de la estación. |
| `checkouts` | Viajes iniciados totales durante la hora. |
| `checkins` | Viajes terminados totales durante la hora. |
| `net_delta` | Flujo neto de la estación durante la hora. |
| `estimated_bikes_available` | Promedio estimado de bicicletas disponibles. |
| `estimated_docks_available` | Promedio estimado de puertos disponibles. |
| `capacity` | Capacidad total de la estación. |
| `temp_c` | Temperatura en grados Celsius. |
| `precip_mm` | Precipitación en milímetros. |
| `station_state` | Estado simulado por hora consolidado (NORMAL, STARVED, OVERFLOW, REBALANCED_REFILL, REBALANCED_DEPLETE). |

### Muestra de Datos (Sample)
| hour | station_id | checkouts | checkins | net_delta | estimated_bikes_available | estimated_docks_available | capacity | temp_c | precip_mm | station_state |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-05-01 00:00:00.000000 | 11 | 0 | 2 | 2 | 14.0 | 13.0 | 27 |  | 0.0 | NORMAL |
| 2026-05-01 01:00:00.000000 | 11 | 0 | 0 | 0 | 14.0 | 13.0 | 27 |  | 0.0 | NORMAL |
| 2026-05-01 02:00:00.000000 | 11 | 0 | 0 | 0 | 14.0 | 13.0 | 27 |  | 0.0 | NORMAL |

---
