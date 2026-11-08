-- Recap schema for Tiger Data (TimescaleDB).
-- Runs automatically on startup when DATABASE_URL is set. Safe to re-run.
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- One row per 5-second feature window. No audio, no transcript text.
CREATE TABLE IF NOT EXISTS speech_features (
    time               TIMESTAMPTZ NOT NULL,
    wpm                DOUBLE PRECISION,
    loudness_db        DOUBLE PRECISION,
    breath_group_words DOUBLE PRECISION,
    effort             DOUBLE PRECISION,
    speaking_s         DOUBLE PRECISION,
    confidence         DOUBLE PRECISION,
    score              DOUBLE PRECISION,
    captions_on        BOOLEAN
);
SELECT create_hypertable('speech_features', 'time', if_not_exists => TRUE);

-- Breathing-trainer (EMST-style) sessions.
CREATE TABLE IF NOT EXISTS emst_sessions (
    time         TIMESTAMPTZ NOT NULL,
    reps         INTEGER,
    peak_cmh2o   DOUBLE PRECISION,
    target_cmh2o DOUBLE PRECISION,
    notes        TEXT
);
SELECT create_hypertable('emst_sessions', 'time', if_not_exists => TRUE);

-- Recap text Alex approved for students (his own words only).
CREATE TABLE IF NOT EXISTS recaps (
    time      TIMESTAMPTZ NOT NULL,
    day       TEXT NOT NULL,
    sentences JSONB NOT NULL
);

-- Who Alex shared his data with, and when he changed his mind.
CREATE TABLE IF NOT EXISTS consent_events (
    time         TIMESTAMPTZ NOT NULL,
    party        TEXT,
    granted      BOOLEAN,
    receipt_hash TEXT,
    chain_tx     TEXT
);

-- One row per teaching session (Start -> End): the session report with its
-- measures, guidance and breathing plan. Numbers and guidance only, no transcript.
CREATE TABLE IF NOT EXISTS sessions (
    id      TEXT PRIMARY KEY,
    started TIMESTAMPTZ NOT NULL,
    ended   TIMESTAMPTZ NOT NULL,
    report  JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions (started DESC);

-- Hourly rollup the clinic can query over months without scanning raw rows.
-- Continuous aggregates refresh themselves on the policy below.
CREATE MATERIALIZED VIEW IF NOT EXISTS speech_hourly
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', time) AS bucket,
       avg(wpm)                AS wpm,
       avg(loudness_db)        AS loudness_db,
       avg(breath_group_words) AS breath_group_words,
       avg(score)              AS fatigue_score,
       sum(speaking_s)         AS speaking_s
FROM speech_features
GROUP BY bucket
WITH NO DATA;

SELECT add_continuous_aggregate_policy('speech_hourly',
    start_offset => INTERVAL '3 days',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '30 minutes',
    if_not_exists => TRUE);
