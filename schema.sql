-- =============================================================================
-- Poker Tracker — PostgreSQL schema
--
-- Single source of truth for the database layout. Applied twice, on purpose:
--   - by Postgres itself on first init (docker-entrypoint-initdb.d)
--   - by the watcher on every start, so schema changes reach an existing volume
-- Everything below must therefore stay idempotent.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- tournaments
-- Populated / enriched from *_summary.txt files.
-- A lightweight placeholder row (tournament_id + player only) is inserted
-- by the history importer when the summary hasn't arrived yet; the summary
-- importer fills in the rest via UPSERT.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tournaments (
    tournament_id       VARCHAR(20)     PRIMARY KEY,
    name                VARCHAR(100),
    player              VARCHAR(50),
    started_at          TIMESTAMP,

    -- Buy-in breakdown
    buyin_prize         NUMERIC(8,2),   -- share going to the prize pool
    buyin_bounty        NUMERIC(8,2),   -- share going to the bounty pool
    buyin_rake          NUMERIC(8,2),   -- platform fee
    buyin_total         NUMERIC(8,2),   -- prize + bounty + rake

    -- Format & field
    players_registered  INT,
    mode                VARCHAR(20),    -- e.g. "tt"
    type                VARCHAR(20),    -- "knockout" | "regular"
    speed               VARCHAR(20),    -- "turbo" | "semiturbo" | "regular"
    prizepool           NUMERIC(10,2),

    -- Session
    -- entries: how many summary blocks the file holds, i.e. how many times the
    -- player bought into this tournament. The buy-in columns above are the
    -- total actually paid, all entries included.
    entries             INT             DEFAULT 1,
    duration_seconds    INT,

    -- Result
    finish_position     INT,
    prize_won           NUMERIC(8,2),
    bounty_won          NUMERIC(8,2),
    total_won           NUMERIC(8,2),
    roi                 NUMERIC(8,2)    -- (total_won - buyin_total) / buyin_total * 100
);

-- ---------------------------------------------------------------------------
-- hands
-- One row per hand played by the hero, populated from hand-history .txt files.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hands (
    hand_id             VARCHAR(60)     PRIMARY KEY,
    tournament_id       VARCHAR(20)     NOT NULL
                            REFERENCES tournaments(tournament_id)
                            ON DELETE CASCADE,
    table_name          VARCHAR(120),   -- nom du tournoi + "(id)#siège"

    -- Blind level at the time of the hand
    level               SMALLINT,
    ante                INT,
    small_blind         INT,
    big_blind           INT,
    hand_datetime       TIMESTAMP,

    -- Hero info
    hero                VARCHAR(50),
    hero_seat           SMALLINT,
    hero_position       VARCHAR(10),    -- BTN | CO | HJ | MP | UTG | UTG+1 | SB | BB
    hero_cards          VARCHAR(10),    -- e.g. "AhKc"
    starting_chips      BIGINT,

    -- Street flags
    saw_flop            BOOLEAN         DEFAULT FALSE,
    went_to_showdown    BOOLEAN         DEFAULT FALSE,
    won_hand            BOOLEAN         DEFAULT FALSE,

    -- Chip accounting
    --   chips_invested = every chip that left the hero's stack: antes, blinds,
    --                    bets, calls, and — for a raise — the "to" total minus
    --                    what was already committed on that street, which is
    --                    NOT the increment Winamax prints after "raises"
    --   net_chips      = chips_won - chips_invested
    chips_won           BIGINT          DEFAULT 0,
    chips_invested      BIGINT          DEFAULT 0,
    net_chips           BIGINT          DEFAULT 0,

    -- Preflop stats
    vpip                BOOLEAN         DEFAULT FALSE,  -- Voluntarily Put $ In Pot
    pfr                 BOOLEAN         DEFAULT FALSE,  -- Pre-Flop Raise

    -- Situational stats. Each rate is stored as the pair (opportunity, action)
    -- rather than a per-hand percentage: the session figure is
    -- SUM(action) / SUM(opportunity), and averaging per-hand percentages —
    -- which is what a single column would force — gives a different, wrong
    -- number as soon as the hands don't all carry the same weight.
    pf_3bet_opp         BOOLEAN         DEFAULT FALSE,  -- faced a lone open raise
    pf_3bet             BOOLEAN         DEFAULT FALSE,  -- …and re-raised
    faced_3bet          BOOLEAN         DEFAULT FALSE,  -- hero opened, got re-raised
    folded_to_3bet      BOOLEAN         DEFAULT FALSE,  -- …and folded
    cbet_flop_opp       BOOLEAN         DEFAULT FALSE,  -- pre-flop aggressor, first to act on the flop
    cbet_flop           BOOLEAN         DEFAULT FALSE,  -- …and bet

    -- Post-flop action counts feeding the aggression factor
    --   AF        = (bets + raises) / calls
    --   frequency = (bets + raises) / (bets + raises + calls + folds)
    postflop_bets       SMALLINT        DEFAULT 0,
    postflop_raises     SMALLINT        DEFAULT 0,
    postflop_calls      SMALLINT        DEFAULT 0,
    postflop_folds      SMALLINT        DEFAULT 0
);

-- A table name is the tournament's own name followed by "(id)#seat", which
-- passes 60 characters on the longer titles — "#1 - W ISLANDS - TROPICAL
-- FLIGHT - SPACE KO - DAY 1(1144676686)#003" needs 67. The insert of a whole
-- batch failed on those, so every hand of the file was lost, not just the one.
-- Widening a varchar in place costs nothing: no rewrite, no scan.
ALTER TABLE hands ALTER COLUMN table_name TYPE VARCHAR(120);

-- Column added by parser version 3, for databases created before it.
ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS entries INT DEFAULT 1;

-- Columns added by parser version 2, for databases created before it.
ALTER TABLE hands ADD COLUMN IF NOT EXISTS pf_3bet_opp     BOOLEAN  DEFAULT FALSE;
ALTER TABLE hands ADD COLUMN IF NOT EXISTS pf_3bet         BOOLEAN  DEFAULT FALSE;
ALTER TABLE hands ADD COLUMN IF NOT EXISTS faced_3bet      BOOLEAN  DEFAULT FALSE;
ALTER TABLE hands ADD COLUMN IF NOT EXISTS folded_to_3bet  BOOLEAN  DEFAULT FALSE;
ALTER TABLE hands ADD COLUMN IF NOT EXISTS cbet_flop_opp   BOOLEAN  DEFAULT FALSE;
ALTER TABLE hands ADD COLUMN IF NOT EXISTS cbet_flop       BOOLEAN  DEFAULT FALSE;
ALTER TABLE hands ADD COLUMN IF NOT EXISTS postflop_bets   SMALLINT DEFAULT 0;
ALTER TABLE hands ADD COLUMN IF NOT EXISTS postflop_raises SMALLINT DEFAULT 0;
ALTER TABLE hands ADD COLUMN IF NOT EXISTS postflop_calls  SMALLINT DEFAULT 0;
ALTER TABLE hands ADD COLUMN IF NOT EXISTS postflop_folds  SMALLINT DEFAULT 0;

-- ---------------------------------------------------------------------------
-- imported_files
-- Records the state of each file at its last successful import. A file is
-- re-imported whenever its size or mtime changed, which is what lets a
-- tournament still in progress be picked up again as Winamax appends to it.
-- The row is only written after a successful commit; failures leave the
-- previous state in place so the next event retries automatically.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS imported_files (
    filepath            TEXT            PRIMARY KEY,
    file_type           VARCHAR(20),    -- 'summary' | 'history'
    file_size           BIGINT,         -- os.stat().st_size at import time
    file_mtime          DOUBLE PRECISION, -- os.stat().st_mtime at import time
    parser_version      INT,            -- parser.PARSER_VERSION at import time
    imported_at         TIMESTAMP       DEFAULT NOW()
);

-- Backfill the fingerprint columns on databases created before they existed.
ALTER TABLE imported_files ADD COLUMN IF NOT EXISTS file_size      BIGINT;
ALTER TABLE imported_files ADD COLUMN IF NOT EXISTS file_mtime     DOUBLE PRECISION;
-- Left NULL on rows imported before versioning existed, which is exactly what
-- makes them compare unequal to the current version and get re-read once.
ALTER TABLE imported_files ADD COLUMN IF NOT EXISTS parser_version INT;

-- ---------------------------------------------------------------------------
-- session_exports
-- One row per session workbook written to disk. The three counts form a
-- content fingerprint: a late-arriving summary or extra hands change them, and
-- the exporter rewrites the file rather than leaving a stale one behind.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS session_exports (
    session_id          TEXT            PRIMARY KEY,
    filepath            TEXT,
    tournaments_count   INT,
    hands_count         INT,
    summaries_count     INT,            -- tournaments whose summary has landed
    exported_at         TIMESTAMP       DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_hands_tournament   ON hands (tournament_id);
CREATE INDEX IF NOT EXISTS idx_hands_datetime     ON hands (hand_datetime);
CREATE INDEX IF NOT EXISTS idx_hands_position     ON hands (hero_position);
CREATE INDEX IF NOT EXISTS idx_tournaments_type   ON tournaments (type, speed);
CREATE INDEX IF NOT EXISTS idx_tournaments_player ON tournaments (player);

-- ---------------------------------------------------------------------------
-- Useful views
-- Dropped and recreated rather than CREATE OR REPLACE'd: the latter refuses
-- any change to the column list, which would break the re-apply on start.
-- ---------------------------------------------------------------------------

-- Per-tournament summary with hand counts and key stats
DROP VIEW IF EXISTS v_tournament_stats;
CREATE VIEW v_tournament_stats AS
SELECT
    t.tournament_id,
    t.name,
    t.player,
    t.started_at,
    t.type,
    t.speed,
    t.buyin_total,
    t.players_registered,
    t.finish_position,
    ROUND(t.finish_position::NUMERIC / NULLIF(t.players_registered, 0) * 100, 1) AS finish_pct,
    t.total_won,
    t.roi,
    t.duration_seconds,

    -- Hand counts from the history file
    COUNT(h.hand_id)                                                AS hands_played,
    ROUND(AVG(h.vpip::INT)  * 100, 1)                              AS vpip_pct,
    ROUND(AVG(h.pfr::INT)   * 100, 1)                              AS pfr_pct,
    ROUND(AVG(h.saw_flop::INT) * 100, 1)                           AS flop_seen_pct,
    COUNT(h.hand_id) FILTER (WHERE h.went_to_showdown)             AS showdowns,
    ROUND(
        AVG(h.won_hand::INT) FILTER (WHERE h.went_to_showdown) * 100, 1
    )                                                               AS wtsd_won_pct,

    -- Situational rates: actions over opportunities, never an average of
    -- per-hand percentages.
    ROUND(
        SUM(h.pf_3bet::INT)::NUMERIC / NULLIF(SUM(h.pf_3bet_opp::INT), 0) * 100, 1
    )                                                               AS pf_3bet_pct,
    ROUND(
        SUM(h.folded_to_3bet::INT)::NUMERIC / NULLIF(SUM(h.faced_3bet::INT), 0) * 100, 1
    )                                                               AS fold_to_3bet_pct,
    ROUND(
        SUM(h.cbet_flop::INT)::NUMERIC / NULLIF(SUM(h.cbet_flop_opp::INT), 0) * 100, 1
    )                                                               AS cbet_flop_pct,
    ROUND(
        (SUM(h.postflop_bets) + SUM(h.postflop_raises))::NUMERIC
        / NULLIF(SUM(h.postflop_calls), 0), 2
    )                                                               AS aggression_factor

FROM tournaments t
LEFT JOIN hands h USING (tournament_id)
GROUP BY t.tournament_id;

-- Overall player stats across all tournaments of a given type / speed
DROP VIEW IF EXISTS v_global_stats;
CREATE VIEW v_global_stats AS
SELECT
    player,
    type,
    speed,
    COUNT(*)                                        AS tournaments,
    SUM(buyin_total)                                AS total_invested,
    SUM(total_won)                                  AS total_won,
    ROUND(SUM(total_won) - SUM(buyin_total), 2)     AS net_profit,
    ROUND(
        (SUM(total_won) - SUM(buyin_total))
        / NULLIF(SUM(buyin_total), 0) * 100, 1
    )                                               AS roi_pct,
    ROUND(AVG(roi), 1)                              AS avg_roi_pct,
    ROUND(AVG(finish_position::NUMERIC
          / NULLIF(players_registered, 0) * 100), 1) AS avg_finish_pct,
    SUM(duration_seconds) / 3600.0                  AS total_hours
FROM tournaments
WHERE buyin_total IS NOT NULL
GROUP BY player, type, speed
ORDER BY roi_pct DESC;

-- Per-position breakdown (VPIP, PFR, net chips)
DROP VIEW IF EXISTS v_position_stats;
CREATE VIEW v_position_stats AS
SELECT
    h.hero                                          AS player,
    h.hero_position                                 AS position,
    COUNT(*)                                        AS hands,
    ROUND(AVG(h.vpip::INT)  * 100, 1)              AS vpip_pct,
    ROUND(AVG(h.pfr::INT)   * 100, 1)              AS pfr_pct,
    ROUND(AVG(h.saw_flop::INT) * 100, 1)           AS flop_pct,
    SUM(h.net_chips)                                AS total_net_chips,
    ROUND(AVG(h.net_chips), 0)                      AS avg_net_chips
FROM hands h
WHERE h.hero_position IS NOT NULL
GROUP BY h.hero, h.hero_position
ORDER BY h.hero,
    ARRAY_POSITION(ARRAY['BTN','CO','HJ','MP','UTG+1','UTG','SB','BB'], h.hero_position);

-- ---------------------------------------------------------------------------
-- Sessions
--
-- A session is a stretch of uninterrupted play, not a tournament and not a
-- calendar day: tournaments overlap when multi-tabling, so the boundaries come
-- from merging their activity windows and cutting wherever the gap between one
-- window ending and the next starting exceeds `gap`.
--
-- Dropped in reverse dependency order — a view holding a function open would
-- block the DROP FUNCTION on re-apply.
-- ---------------------------------------------------------------------------
DROP VIEW     IF EXISTS v_sessions;
DROP VIEW     IF EXISTS v_session_tournaments;
DROP FUNCTION IF EXISTS f_session_map(INTERVAL);

CREATE FUNCTION f_session_map(gap INTERVAL DEFAULT INTERVAL '45 minutes')
RETURNS TABLE (
    tournament_id   VARCHAR(20),
    player          VARCHAR(50),
    session_id      TEXT,
    session_start   TIMESTAMP,
    session_end     TIMESTAMP
)
LANGUAGE sql STABLE AS $$
    -- Activity window per tournament. Hand timestamps are the ground truth for
    -- when the hero was actually at the table; the summary is only a fallback
    -- for a tournament whose history hasn't been imported.
    WITH windows AS (
        SELECT
            t.tournament_id,
            t.player,
            COALESCE(h.first_hand, t.started_at) AS ts_start,
            COALESCE(
                h.last_hand,
                t.started_at + make_interval(secs => COALESCE(t.duration_seconds, 0))
            )                                    AS ts_end
        FROM tournaments t
        LEFT JOIN (
            SELECT tournament_id,
                   MIN(hand_datetime) AS first_hand,
                   MAX(hand_datetime) AS last_hand
            FROM hands
            GROUP BY tournament_id
        ) h USING (tournament_id)
        WHERE COALESCE(h.first_hand, t.started_at) IS NOT NULL
    ),
    -- Running max of every *preceding* end, so overlapping tournaments merge
    -- instead of each opening a session. NULL on the first row of a player,
    -- which is what opens the very first one.
    marked AS (
        SELECT *,
            CASE
                WHEN MAX(ts_end) OVER w IS NULL          THEN 1
                WHEN ts_start - MAX(ts_end) OVER w > gap THEN 1
                ELSE 0
            END AS starts_session
        FROM windows
        WINDOW w AS (
            PARTITION BY player ORDER BY ts_start, tournament_id
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        )
    ),
    islands AS (
        SELECT *,
            SUM(starts_session) OVER (
                PARTITION BY player ORDER BY ts_start, tournament_id
                ROWS UNBOUNDED PRECEDING
            ) AS island
        FROM marked
    ),
    bounds AS (
        SELECT player, island,
               MIN(ts_start) AS session_start,
               MAX(ts_end)   AS session_end
        FROM islands
        GROUP BY player, island
    )
    SELECT
        i.tournament_id,
        i.player,
        -- Stable, human-readable key. Timestamps are stored as naive UTC, so
        -- the local label has to be derived explicitly — a session starting at
        -- 23:30 Paris time falls on the next day in UTC.
        to_char(
            b.session_start AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Paris',
            'YYYY-MM-DD_HH24MI'
        ) || '_' || i.player AS session_id,
        b.session_start,
        b.session_end
    FROM islands i
    JOIN bounds b USING (player, island);
$$;

-- Which tournament belongs to which session, at the default 45-minute cut.
-- The exporter calls f_session_map() directly with its configured gap.
CREATE VIEW v_session_tournaments AS SELECT * FROM f_session_map();

-- One row per session: money, field and volume.
CREATE VIEW v_sessions AS
SELECT
    m.session_id,
    m.player,
    m.session_start,
    m.session_end,
    EXTRACT(EPOCH FROM (m.session_end - m.session_start))::INT      AS elapsed_seconds,
    COUNT(*)                                                        AS tournaments,
    COUNT(*) FILTER (WHERE t.buyin_total IS NOT NULL)               AS summaries,
    COUNT(*) FILTER (WHERE t.total_won > 0)                         AS itm,
    COALESCE(SUM(hc.hands), 0)                                      AS hands,
    SUM(t.buyin_total)                                              AS total_invested,
    SUM(t.total_won)                                                AS total_won,
    ROUND(COALESCE(SUM(t.total_won), 0) - COALESCE(SUM(t.buyin_total), 0), 2)
                                                                    AS net_profit,
    ROUND(
        (COALESCE(SUM(t.total_won), 0) - COALESCE(SUM(t.buyin_total), 0))
        / NULLIF(SUM(t.buyin_total), 0) * 100, 1
    )                                                               AS roi_pct
FROM v_session_tournaments m
JOIN tournaments t USING (tournament_id)
LEFT JOIN (
    SELECT tournament_id, COUNT(*) AS hands FROM hands GROUP BY tournament_id
) hc USING (tournament_id)
GROUP BY m.session_id, m.player, m.session_start, m.session_end
ORDER BY m.session_start DESC;
