-- =============================================================================
-- views.sql — Geospatial Views for Ecobici Data Lake
-- =============================================================================

-- =============================================================================
-- View 1: vw_ecobici_weather_mapping
-- Maps each Ecobici station to its weather data.
-- Since we use Open-Meteo per-station coordinates, each Ecobici station
-- IS its own weather station (station_id is shared across both tables).
-- The view keeps the original interface so rollups.sql doesn't need changing.
-- =============================================================================

CREATE OR REPLACE VIEW ecobici_lake.vw_ecobici_weather_mapping AS
SELECT
    e.station_id    AS ecobici_station_id,
    e.name          AS ecobici_name,
    e.lat           AS ecobici_lat,
    e.lon           AS ecobici_lon,
    e.station_id    AS weather_station_id,  -- same ID — direct match
    e.lat           AS weather_lat,
    e.lon           AS weather_lon,
    0.0             AS distance_m           -- co-located (same station)
FROM ecobici_lake.ecobici_station_info e;


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
