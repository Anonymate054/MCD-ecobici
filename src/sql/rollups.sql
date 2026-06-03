-- =============================================================================
-- rollups.sql — Hourly Station Status Rollup (Athena CTAS)
-- =============================================================================
-- Creates / refreshes the `hourly_station_status` Iceberg table by aggregating
-- raw 5-minute snapshots into hourly summaries with heuristic malfunction
-- detection and nearest-weather-station data joined via the geospatial view.
--
-- Run this query via:
--   aws athena start-query-execution \
--     --query-string file://rollups.sql \
--     --work-group ecobici-workgroup \
--     --query-execution-context Database=ecobici_lake
--
-- NOTE: For incremental refreshes on production, replace the CTAS with an
--   INSERT INTO ... SELECT ... WHERE hour >= DATE_ADD('hour', -2, NOW())
--   to append only the latest window. The CTAS form below is safe for
--   initial population or full reprocessing.
-- =============================================================================

-- Step 1: Compute heuristic broken flag per station per hour
-- ---------------------------------------------------------------
-- A station is flagged `is_heuristically_broken = true` if EITHER:
--   (a) is_renting AND is_returning are both FALSE (native feed flag), OR
--   (b) bikes_available > 0 AND the count has not changed by ±1 for 3
--       consecutive hours during peak time (07:00–21:00 local), while
--       neighboring stations (< 3 km) show high turnover.
--
-- The frozen-count check uses LAG/LEAD over station partitions.

WITH
-- ── Raw 5-min data filtered to the last 48 h for CTAS efficiency ──────────
raw AS (
    SELECT
        date_trunc('hour', "timestamp")       AS hour,
        station_id,
        bikes_available,
        docks_available,
        is_renting,
        is_returning,
        -- Peak hours: 07:00–21:00 local (UTC-6); stored as UTC → UTC+6 offset check
        (EXTRACT(HOUR FROM "timestamp" AT TIME ZONE 'America/Mexico_City')
            BETWEEN 7 AND 20)                 AS is_peak_hour,
        "timestamp"
    FROM ecobici_lake.raw_station_status
    -- For full backfill remove the WHERE clause; for incremental, keep it:
    -- WHERE "timestamp" >= DATE_ADD('hour', -48, NOW())
),

-- ── Hourly aggregates ─────────────────────────────────────────────────────
hourly_agg AS (
    SELECT
        hour,
        station_id,
        ROUND(AVG(CAST(bikes_available AS DOUBLE)), 2)   AS avg_bikes_available,
        ROUND(AVG(CAST(docks_available AS DOUBLE)), 2)   AS avg_docks_available,
        -- Minutes renting/returning: each 5-min record = 5 minutes
        SUM(CASE WHEN is_renting   THEN 5 ELSE 0 END)   AS total_renting_minutes,
        SUM(CASE WHEN is_returning THEN 5 ELSE 0 END)   AS total_returning_minutes,
        -- Native malfunction flag: station was never renting AND never returning
        BOOL_AND(NOT is_renting AND NOT is_returning)   AS native_malfunction,
        -- For frozen-count heuristic: check stddev of bikes_available
        STDDEV(CAST(bikes_available AS DOUBLE))          AS bikes_stddev,
        AVG(CAST(bikes_available AS DOUBLE))             AS avg_bikes,
        MAX(CASE WHEN is_peak_hour THEN 1 ELSE 0 END)   AS during_peak
    FROM raw
    GROUP BY 1, 2
),

-- ── Rolling 3-hour frozen-count window ────────────────────────────────────
-- A station is "frozen" if bikes_stddev ≈ 0 (no change) across 3 consecutive
-- hours AND bikes_available > 0 AND it's peak time.
frozen_window AS (
    SELECT
        hour,
        station_id,
        avg_bikes,
        bikes_stddev,
        during_peak,
        native_malfunction,
        avg_bikes_available,
        avg_docks_available,
        total_renting_minutes,
        total_returning_minutes,
        -- Look at the previous 2 hours; flag if ALL 3 hours have stddev < 0.5
        LAG(bikes_stddev, 1) OVER w AS stddev_lag1,
        LAG(bikes_stddev, 2) OVER w AS stddev_lag2,
        LAG(avg_bikes,    1) OVER w AS avg_bikes_lag1,
        LAG(avg_bikes,    2) OVER w AS avg_bikes_lag2
    FROM hourly_agg
    WINDOW w AS (PARTITION BY station_id ORDER BY hour ASC)
),

-- ── Neighbor turnover: average stddev of stations within 3 km ─────────────
-- Uses the geospatial view to identify neighbors, then measures their mean
-- bikes_stddev as a proxy for "high turnover."
neighbor_turnover AS (
    SELECT
        h.hour,
        h.station_id,
        AVG(n_agg.bikes_stddev) AS neighbor_avg_stddev
    FROM hourly_agg h
    JOIN ecobici_lake.vw_ecobici_weather_mapping vm
        ON vm.ecobici_station_id = h.station_id
    -- Self-join to get neighbor hourly stats for the same hour
    JOIN hourly_agg n_agg
        ON n_agg.hour = h.hour
        AND n_agg.station_id != h.station_id
    -- Inner join to distance table (re-use the weather mapping view which has
    -- ecobici-to-ecobici distances via the same ST_Distance logic)
    JOIN (
        SELECT
            s1.station_id  AS station_id,
            s2.station_id  AS neighbor_id,
            ST_Distance(
                ST_Point(s1.lon, s1.lat),
                ST_Point(s2.lon, s2.lat)
            )              AS distance_m
        FROM ecobici_lake.ecobici_station_info s1
        CROSS JOIN ecobici_lake.ecobici_station_info s2
        WHERE s1.station_id <> s2.station_id
    ) dist
        ON  dist.station_id  = h.station_id
        AND dist.neighbor_id = n_agg.station_id
        AND dist.distance_m  < 3000   -- within 3 km
    GROUP BY 1, 2
),

-- ── Final heuristic flag ──────────────────────────────────────────────────
flagged AS (
    SELECT
        fw.hour,
        fw.station_id,
        fw.avg_bikes_available,
        fw.avg_docks_available,
        fw.total_renting_minutes,
        fw.total_returning_minutes,
        (
            -- Condition (a): native feed malfunction
            fw.native_malfunction
            OR
            -- Condition (b): frozen count during peak + neighbors showing turnover
            (
                fw.during_peak = 1
                AND fw.avg_bikes       > 0
                AND fw.bikes_stddev    < 0.5
                AND fw.stddev_lag1     IS NOT NULL AND fw.stddev_lag1 < 0.5
                AND fw.stddev_lag2     IS NOT NULL AND fw.stddev_lag2 < 0.5
                AND fw.avg_bikes_lag1  IS NOT NULL
                AND fw.avg_bikes_lag2  IS NOT NULL
                AND COALESCE(nt.neighbor_avg_stddev, 0) > 1.0
            )
        )                              AS is_heuristically_broken
    FROM frozen_window fw
    LEFT JOIN neighbor_turnover nt
        ON nt.hour = fw.hour AND nt.station_id = fw.station_id
)

-- ── Final output joined with nearest weather data ─────────────────────────
INSERT INTO ecobici_lake.hourly_station_status
SELECT
    f.hour,
    f.station_id,
    f.avg_bikes_available,
    f.avg_docks_available,
    f.total_renting_minutes,
    f.total_returning_minutes,
    f.is_heuristically_broken,
    -- Join closest weather reading for this hour
    COALESCE(
        w.temp_c,
        LAG(w.temp_c) OVER (PARTITION BY f.station_id ORDER BY f.hour)
    )                                  AS temp_c,
    COALESCE(w.precip_mm, 0.0)         AS precip_mm
FROM flagged f
-- Join via geospatial mapping view to get the nearest weather station
LEFT JOIN ecobici_lake.vw_ecobici_weather_mapping vm
    ON vm.ecobici_station_id = f.station_id
LEFT JOIN (
    SELECT
        date_trunc('hour', "timestamp") AS hour,
        station_id,
        AVG(temp_c)    AS temp_c,
        SUM(precip_mm) AS precip_mm
    FROM ecobici_lake.weather_observations
    WHERE _is_filled = FALSE
    GROUP BY 1, 2
) w
    ON  w.hour       = f.hour
    AND w.station_id = vm.weather_station_id
ORDER BY f.hour, f.station_id;
