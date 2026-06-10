-- =============================================================================
-- rollups.sql — Online Station Status Rollup Strategy
-- =============================================================================
-- Ingests raw 5-minute snapshots into a 15-minute table, and then aggregates
-- the 15-minute intervals into hourly summaries with weather and heuristics.
-- This ensures mathematical consistency and reduces query costs.
-- =============================================================================

-- Step 1: 15-Minute Rollup (raw_station_status -> station_status_15m)
-- -----------------------------------------------------------------------------
-- Truncates 5-minute raw records to 15-minute buckets, flagging transitions
-- for manual rebalancing and calculating average bike/dock counts.
-- -----------------------------------------------------------------------------

INSERT INTO ecobici_lake.station_status_15m
WITH
raw AS (
    SELECT
        date_add('minute', -(minute("timestamp") % 15), date_trunc('minute', "timestamp")) AS interval_start,
        station_id,
        bikes_available,
        docks_available,
        is_renting,
        is_returning,
        "timestamp"
    FROM ecobici_lake.raw_station_status
),
raw_deltas AS (
    SELECT
        interval_start,
        station_id,
        bikes_available,
        bikes_available - LAG(bikes_available, 1) OVER (PARTITION BY station_id ORDER BY "timestamp" ASC) AS delta
    FROM raw
),
rebalance_detect AS (
    SELECT
        interval_start,
        station_id,
        MAX(CASE WHEN delta >= 5 THEN 1 ELSE 0 END) AS has_refill,
        MAX(CASE WHEN delta <= -5 THEN 1 ELSE 0 END) AS has_deplete
    FROM raw_deltas
    GROUP BY 1, 2
),
interval_agg AS (
    SELECT
        interval_start,
        station_id,
        ROUND(AVG(CAST(bikes_available AS DOUBLE)), 2) AS avg_bikes_available,
        ROUND(AVG(CAST(docks_available AS DOUBLE)), 2) AS avg_docks_available,
        SUM(CASE WHEN is_renting THEN 5 ELSE 0 END) AS total_renting_minutes,
        SUM(CASE WHEN is_returning THEN 5 ELSE 0 END) AS total_returning_minutes
    FROM raw
    GROUP BY 1, 2
)
SELECT
    i.interval_start AS timestamp,
    i.station_id,
    i.avg_bikes_available,
    i.avg_docks_available,
    i.total_renting_minutes,
    i.total_returning_minutes,
    CASE 
        WHEN COALESCE(rd.has_refill, 0) = 1 THEN 'REBALANCED_REFILL'
        WHEN COALESCE(rd.has_deplete, 0) = 1 THEN 'REBALANCED_DEPLETE'
        WHEN i.avg_bikes_available = 0 THEN 'STARVED'
        WHEN i.avg_docks_available = 0 THEN 'OVERFLOW'
        ELSE 'NORMAL'
    END AS station_state
FROM interval_agg i
LEFT JOIN rebalance_detect rd
    ON rd.interval_start = i.interval_start AND rd.station_id = i.station_id
ORDER BY i.interval_start, i.station_id;


-- Step 2: Hourly Rollup (station_status_15m + weather -> hourly_station_status)
-- -----------------------------------------------------------------------------
-- Aggregates 15-minute intervals to 1 hour, joining nearest weather data and
-- applying hourly heuristic malfunction checks.
-- -----------------------------------------------------------------------------

INSERT INTO ecobici_lake.hourly_station_status
WITH
raw_15m AS (
    SELECT
        date_trunc('hour', "timestamp")      AS hour,
        station_id,
        avg_bikes_available,
        avg_docks_available,
        total_renting_minutes,
        total_returning_minutes,
        station_state,
        (EXTRACT(HOUR FROM "timestamp" AT TIME ZONE 'America/Mexico_City')
            BETWEEN 7 AND 20)               AS is_peak_hour
    FROM ecobici_lake.station_status_15m
),
hourly_agg AS (
    SELECT
        hour, station_id,
        ROUND(AVG(avg_bikes_available), 2)  AS avg_bikes_available,
        ROUND(AVG(avg_docks_available), 2)  AS avg_docks_available,
        SUM(total_renting_minutes)          AS total_renting_minutes,
        SUM(total_returning_minutes)        AS total_returning_minutes,
        STDDEV(avg_bikes_available)         AS bikes_stddev,
        AVG(avg_bikes_available)            AS avg_bikes,
        MAX(CASE WHEN is_peak_hour THEN 1 ELSE 0 END)  AS during_peak,
        CASE 
            WHEN SUM(CASE WHEN station_state = 'REBALANCED_REFILL' THEN 1 ELSE 0 END) > 0 THEN 'REBALANCED_REFILL'
            WHEN SUM(CASE WHEN station_state = 'REBALANCED_DEPLETE' THEN 1 ELSE 0 END) > 0 THEN 'REBALANCED_DEPLETE'
            WHEN SUM(CASE WHEN station_state = 'STARVED' THEN 1 ELSE 0 END) > 0 THEN 'STARVED'
            WHEN SUM(CASE WHEN station_state = 'OVERFLOW' THEN 1 ELSE 0 END) > 0 THEN 'OVERFLOW'
            ELSE 'NORMAL'
        END AS raw_state
    FROM raw_15m
    GROUP BY 1, 2
),
frozen_window AS (
    SELECT
        hour, station_id,
        avg_bikes, bikes_stddev, during_peak, raw_state,
        avg_bikes_available, avg_docks_available,
        total_renting_minutes, total_returning_minutes,
        LAG(bikes_stddev, 1) OVER (PARTITION BY station_id ORDER BY hour) AS stddev_lag1,
        LAG(bikes_stddev, 2) OVER (PARTITION BY station_id ORDER BY hour) AS stddev_lag2,
        LAG(avg_bikes,    1) OVER (PARTITION BY station_id ORDER BY hour) AS avg_bikes_lag1,
        LAG(avg_bikes,    2) OVER (PARTITION BY station_id ORDER BY hour) AS avg_bikes_lag2
    FROM hourly_agg
),
flagged AS (
    SELECT
        hour, station_id,
        avg_bikes_available, avg_docks_available,
        total_renting_minutes, total_returning_minutes,
        (
            (total_renting_minutes = 0 AND total_returning_minutes = 0)
            OR (
                during_peak = 1
                AND avg_bikes    > 0
                AND bikes_stddev < 0.5
                AND stddev_lag1  IS NOT NULL AND stddev_lag1 < 0.5
                AND stddev_lag2  IS NOT NULL AND stddev_lag2 < 0.5
                AND avg_bikes_lag1 IS NOT NULL
                AND avg_bikes_lag2 IS NOT NULL
            )
        ) AS is_heuristically_broken,
        raw_state
    FROM frozen_window
),
weather_hourly AS (
    SELECT
        date_trunc('hour', "timestamp") AS hour,
        station_id,
        AVG(temp_c)    AS temp_c,
        SUM(precip_mm) AS precip_mm
    FROM ecobici_lake.weather_observations
    WHERE _is_filled = FALSE
    GROUP BY 1, 2
)
SELECT
    f.hour,
    f.station_id,
    f.avg_bikes_available,
    f.avg_docks_available,
    f.total_renting_minutes,
    f.total_returning_minutes,
    f.is_heuristically_broken,
    COALESCE(
        w.temp_c,
        LAG(w.temp_c) OVER (PARTITION BY f.station_id ORDER BY f.hour)
    ) AS temp_c,
    COALESCE(w.precip_mm, 0.0) AS precip_mm,
    CASE 
        WHEN f.is_heuristically_broken THEN 'BROKEN'
        ELSE f.raw_state
    END AS station_state
FROM flagged f
-- Join via weather mapping to find nearest weather station
LEFT JOIN ecobici_lake.vw_ecobici_weather_mapping vm
    ON vm.ecobici_station_id = f.station_id
LEFT JOIN weather_hourly w
    ON  w.hour       = f.hour
    AND w.station_id = vm.weather_station_id
ORDER BY f.hour, f.station_id;

