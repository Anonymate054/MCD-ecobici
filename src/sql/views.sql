-- =============================================================================
-- views.sql — Geospatial Materialized Views
-- =============================================================================

-- =============================================================================
-- View 1: vw_ecobici_weather_mapping
-- Maps each Ecobici station to its nearest weather station via ST_Distance.
-- =============================================================================

CREATE OR REPLACE VIEW ecobici_lake.vw_ecobici_weather_mapping AS
WITH distances AS (
    SELECT
        e.station_id                   AS ecobici_station_id,
        e.name                         AS ecobici_name,
        e.lat                          AS ecobici_lat,
        e.lon                          AS ecobici_lon,
        w.station_id                   AS weather_station_id,
        w.lat                          AS weather_lat,
        w.lon                          AS weather_lon,
        ST_Distance(
            ST_Point(e.lon, e.lat),
            ST_Point(w.lon, w.lat)
        ) * 111139                     AS distance_m  -- degrees → meters (~lat CDMX)
    FROM ecobici_lake.ecobici_station_info e
    CROSS JOIN (
        -- Replace with a dedicated weather_station_info table when available
        SELECT DISTINCT station_id, 0.0 AS lat, 0.0 AS lon
        FROM ecobici_lake.weather_observations
    ) w
),
ranked AS (
    SELECT *,
        ROW_NUMBER() OVER (
            PARTITION BY ecobici_station_id
            ORDER BY distance_m ASC
        ) AS rank
    FROM distances
)
SELECT
    ecobici_station_id,
    ecobici_name,
    ecobici_lat,
    ecobici_lon,
    weather_station_id,
    weather_lat,
    weather_lon,
    ROUND(distance_m, 1) AS distance_m
FROM ranked
WHERE rank = 1;


-- =============================================================================
-- View 2: vw_broken_stations_summary
-- Daily summary of heuristically broken stations + total bikes available.
-- total_bikes_available = 0 triggers the CloudWatch data drift alarm.
-- =============================================================================

CREATE OR REPLACE VIEW ecobici_lake.vw_broken_stations_summary AS
SELECT
    CAST(date_trunc('day', hour) AS DATE)      AS day,
    COUNT(DISTINCT station_id)                 AS total_stations,
    COUNT(DISTINCT CASE
        WHEN is_heuristically_broken THEN station_id END) AS broken_stations,
    ROUND(
        100.0 * COUNT(DISTINCT CASE
            WHEN is_heuristically_broken THEN station_id END)
        / NULLIF(COUNT(DISTINCT station_id), 0), 2
    )                                          AS broken_pct,
    ROUND(SUM(avg_bikes_available), 0)         AS total_bikes_available
FROM ecobici_lake.hourly_station_status
GROUP BY 1
ORDER BY 1 DESC;
