"""
Session-level queries.

A session is a stretch of uninterrupted play — see f_session_map in schema.sql
for how the boundaries are derived. Everything here takes the gap as a
parameter rather than relying on the 45-minute default baked into the
v_sessions view, so SESSION_GAP_MINUTES stays the single knob.

Rates are computed as SUM(action) / SUM(opportunity) and returned as fractions
in 0..1, ready for a percentage cell format. Averaging the per-hand booleans
would only be correct where every hand carries an opportunity, which is true
for VPIP and PFR and false for everything else.

Four things here exist because these are tournaments and not cash games:

  - results are also expressed in buy-ins, the only unit that survives a
    session mixing a 2 € and a 20 € tournament
  - the money is framed against the variance the player's own history shows,
    because twenty MTTs settle almost nothing (see _variance)
  - stacks are measured in big blinds and split by depth, because a hand at
    50 bb and a hand at 8 bb belong to two different games (see _depths)
  - every money figure is split by format, whose payout shapes are too
    different to be added together (see _FAMILY)

Denominators are returned next to the rates they feed, so the workbook can say
how much a number is worth instead of printing a confident-looking percentage
built on nine hands.
"""

from datetime import timedelta

from psycopg2.extras import RealDictCursor

DEFAULT_GAP_MINUTES = 45

# The families the workbook keeps apart, in the order it lists them.
#
# The three MTT ones exist because their payouts have different shapes. Winamax
# only prints "Type : knockout", which lumps mystery bounties in with ordinary
# ones: they behave nothing alike, since a mystery holds its bounty pool back
# for the players who survive to the draw, so nearly all of its return sits in
# a tail a session will almost never contain. The name is what carries it.
#
# 'sng' is not a payout shape but a different game: three-handed hyper-turbos
# whose field of 3 makes every MTT measure here meaningless — a 33 % ITM, a
# "top 10 % of the field" that cannot exist, a stack depth on a structure with
# no middle. Their money still counts, so they keep a column of their own on
# the Formats sheet and stay out of everything else.
#
# 'inconnu' is a tournament whose summary has not landed: no buy-in, no type,
# no name. Counting it as vanilla would quietly pad the format that happens to
# be the default.
FAMILIES = ("vanilla", "knockout", "mystery", "sng", "inconnu")
MTT_FAMILIES = ("vanilla", "knockout", "mystery")

# Winamax's "Type :" values that are not multi-table tournaments — Expresso and
# its cousins.
SNG_TYPES = ("sitngo", "madtilt", "wys")

_FAMILY = f"""(
    CASE
        WHEN t.buyin_total IS NULL AND t.type IS NULL  THEN 'inconnu'
        WHEN t.type IN ({', '.join(repr(t) for t in SNG_TYPES)})
                                                      THEN 'sng'
        WHEN t.name ~* '(mystery|myst[eè]re|\\ymko\\y)' THEN 'mystery'
        -- The name matters as much as the type here: a day 2 carries no buy-in
        -- and therefore no bounty share, so only "KO" in its name still says
        -- what tournament it is.
        WHEN t.type = 'knockout'
             OR COALESCE(t.buyin_bounty, 0) > 0
             OR t.name ~* '\\yko\\y'                         THEN 'knockout'
        ELSE 'vanilla'
    END
)"""

# A finish inside the top DEEP_RUN_PCT of the field is where MTT money actually
# comes from; FINAL_TABLE_PLACES is the usual nine-handed last table.
DEEP_RUN_PCT = 0.10
FINAL_TABLE_PLACES = 9

# Busting deeper than this is the most actionable line in the workbook: at 8 bb
# the stack played itself, at 30 bb a pot was chosen and lost.
DEEP_EXIT_BB = 25

# Stack depths that behave like separate games, deepest first. The last
# threshold has to be 0 — it is the catch-all.
DEPTH_BUCKETS = (
    (40, "40 bb et plus"),
    (20, "de 20 à 40 bb"),
    (10, "de 10 à 20 bb"),
    (0,  "moins de 10 bb"),
)

# Below this many tournaments a player's own standard deviation is itself
# noise, and DEFAULT_SD_BUYINS — a middling figure for large-field MTTs —
# stands in, flagged so the sheet can say the number is borrowed.
MIN_VARIANCE_SAMPLE = 50
DEFAULT_SD_BUYINS = 2.0

# The tournaments of one session; the sub-query every scope below is built on.
_SESSION_TOURNAMENTS = """
    SELECT tournament_id FROM f_session_map(%(gap)s) WHERE session_id = %(sid)s
"""

_IN_SESSION = f"tournament_id IN ({_SESSION_TOURNAMENTS})"

# Hands played in a multi-table tournament, whatever the session. Everything
# that describes play — the rates, the depth split, the positions, the biggest
# pots — is restricted to these: an Expresso is three-handed and ten minutes
# long, and averaging it into an MTT line describes neither.
_MTT_TOURNAMENTS = f"SELECT tournament_id FROM tournaments t WHERE {_FAMILY} <> 'sng'"
_IN_MTT = f"tournament_id IN ({_MTT_TOURNAMENTS})"

_SESSION_MTT_HANDS = f"{_IN_SESSION} AND {_IN_MTT}"

# Hands of one session, grouped per tournament. Used wherever a query needs
# both money (on tournaments) and play (on hands) in the same row.
_SESSION_HAND_COUNTS = """
    SELECT
        tournament_id,
        COUNT(*)               AS hands,
        SUM(vpip::INT)         AS vpip,
        SUM(pfr::INT)          AS pfr,
        SUM(saw_flop::INT)     AS flop,
        SUM(pf_3bet::INT)      AS three_bet,
        SUM(pf_3bet_opp::INT)  AS three_bet_opp
    FROM hands
    WHERE {scope}
    GROUP BY tournament_id
"""

# Aggregate shared by due_sessions() and fetch_session(): the shape of a session
# before any of its details are pulled in.
_SESSION_AGG = f"""
    SELECT
        m.session_id,
        m.player,
        m.session_start,
        m.session_end,
        COUNT(*)                                                      AS tournaments_count,
        COUNT(*) FILTER (WHERE t.buyin_total IS NOT NULL)             AS summaries_count,
        -- Everything below counts multi-table tournaments only: a three-handed
        -- Expresso cashes one time in three and has no field to finish deep in,
        -- so mixing it in would move every one of these figures for reasons
        -- that have nothing to do with how the MTTs went.
        COUNT(*) FILTER (WHERE {_FAMILY} <> 'sng')                    AS mtt_count,
        -- In the money means the prize pool paid. Cashing is not the same
        -- thing: a knockout hands out bounties long before the bubble, so
        -- counting "won something" as ITM would flatter every KO session.
        COUNT(*) FILTER (WHERE t.prize_won > 0 AND {_FAMILY} <> 'sng')  AS itm,
        COUNT(*) FILTER (WHERE t.total_won > 0 AND {_FAMILY} <> 'sng')  AS cashed,
        COUNT(*) FILTER (
            WHERE t.finish_position <= {FINAL_TABLE_PLACES} AND {_FAMILY} <> 'sng'
        )                                                             AS final_tables,
        COUNT(*) FILTER (
            WHERE t.finish_position::NUMERIC
                  / NULLIF(t.players_registered, 0) <= {DEEP_RUN_PCT}
              AND {_FAMILY} <> 'sng'
        )                                                             AS deep_runs,
        -- Median, not mean: a min-cash and a final table do not average into
        -- anything, and one deep run would drag the mean across the session.
        PERCENTILE_CONT(0.5) WITHIN GROUP (
            ORDER BY t.finish_position::NUMERIC / NULLIF(t.players_registered, 0)
        ) FILTER (WHERE {_FAMILY} <> 'sng')                           AS median_finish_pct,
        MIN(t.finish_position::NUMERIC / NULLIF(t.players_registered, 0))
            FILTER (WHERE {_FAMILY} <> 'sng')                         AS best_finish_pct,
        SUM(COALESCE(t.entries, 1) - 1)                               AS reentries,
        -- The total drives the export fingerprint, so it counts every hand;
        -- the MTT figure is the one the sheets reason about.
        COALESCE(SUM(hc.hands), 0)::INT                               AS hands_count,
        COALESCE(SUM(hc.hands) FILTER (WHERE {_FAMILY} <> 'sng'), 0)::INT
                                                                      AS mtt_hands_count,
        SUM(t.buyin_total)                                            AS total_invested,
        SUM(t.total_won)                                              AS total_won,
        SUM(t.prize_won)                                              AS prize_won,
        SUM(t.bounty_won)                                             AS bounty_won,
        SUM(t.buyin_bounty)                                           AS bounty_invested,
        SUM(t.buyin_rake)                                             AS rake,
        -- The buy-in level actually played, which is what the bankroll rules
        -- and the "in buy-ins" figures divide by. Free entries are left out on
        -- purpose: a freeroll, a ticket and a day 2 all cost nothing, and
        -- averaging them in shrinks the divisor until a normal night looks
        -- like a huge swing in buy-ins.
        COUNT(*) FILTER (WHERE t.buyin_total > 0)                     AS paid_count,
        AVG(t.buyin_total) FILTER (WHERE t.buyin_total > 0)           AS avg_buyin,
        AVG(t.players_registered)                                     AS avg_field,
        ROUND(COALESCE(SUM(t.total_won), 0)
              - COALESCE(SUM(t.buyin_total), 0), 2)                   AS net_profit,
        SUM(t.duration_seconds)                                       AS played_seconds
    FROM f_session_map(%(gap)s) m
    JOIN tournaments t USING (tournament_id)
    LEFT JOIN (
        SELECT tournament_id, COUNT(*) AS hands FROM hands GROUP BY tournament_id
    ) hc USING (tournament_id)
    GROUP BY m.session_id, m.player, m.session_start, m.session_end
"""

# The rate block, applied either to one session or to a player's whole history.
# `{scope}` is a WHERE clause, never user input. Each rate is followed by the
# denominator it was computed on, which is what the workbook needs to tell a
# read from a coin flip.
_RATES = """
    SELECT
        COUNT(*)                                                      AS hands,
        AVG(vpip::INT)                                                AS vpip,
        AVG(pfr::INT)                                                 AS pfr,
        AVG(saw_flop::INT)                                            AS flop,
        SUM(went_to_showdown::INT)::NUMERIC
            / NULLIF(SUM(saw_flop::INT), 0)                           AS wtsd,
        SUM((won_hand AND went_to_showdown)::INT)::NUMERIC
            / NULLIF(SUM(went_to_showdown::INT), 0)                   AS wsd,
        SUM(pf_3bet::INT)::NUMERIC
            / NULLIF(SUM(pf_3bet_opp::INT), 0)                        AS three_bet,
        SUM(folded_to_3bet::INT)::NUMERIC
            / NULLIF(SUM(faced_3bet::INT), 0)                         AS fold_to_3bet,
        SUM(cbet_flop::INT)::NUMERIC
            / NULLIF(SUM(cbet_flop_opp::INT), 0)                      AS cbet_flop,
        (SUM(postflop_bets) + SUM(postflop_raises))::NUMERIC
            / NULLIF(SUM(postflop_calls), 0)                          AS aggression_factor,
        (SUM(postflop_bets) + SUM(postflop_raises))::NUMERIC
            / NULLIF(SUM(postflop_bets) + SUM(postflop_raises)
                     + SUM(postflop_calls) + SUM(postflop_folds), 0)  AS aggression_freq,
        SUM(saw_flop::INT)                                            AS n_flop,
        SUM(went_to_showdown::INT)                                    AS n_showdown,
        SUM(pf_3bet_opp::INT)                                         AS n_3bet_opp,
        SUM(faced_3bet::INT)                                          AS n_faced_3bet,
        SUM(cbet_flop_opp::INT)                                       AS n_cbet_opp,
        SUM(postflop_calls)                                           AS n_calls,
        SUM(postflop_bets) + SUM(postflop_raises)
            + SUM(postflop_calls) + SUM(postflop_folds)               AS n_postflop
    FROM hands
    WHERE {scope}
"""

# Depth-bucket CASEs, built from DEPTH_BUCKETS so thresholds and labels stay in
# one place. Values are module constants, never user input.
_DEPTH_ORD = "CASE " + " ".join(
    f"WHEN depth >= {threshold} THEN {i}" for i, (threshold, _) in enumerate(DEPTH_BUCKETS)
) + " END"
_DEPTH_LABEL = "CASE " + " ".join(
    f"WHEN depth >= {threshold} THEN '{label}'" for threshold, label in DEPTH_BUCKETS
) + " END"

# Play by stack depth, for one session or for the whole history. Same `{scope}`
# convention as _RATES.
_DEPTHS = f"""
    WITH d AS (
        SELECT *, starting_chips::NUMERIC / big_blind AS depth
        FROM hands
        WHERE {{scope}} AND big_blind > 0 AND starting_chips IS NOT NULL
    )
    SELECT
        {_DEPTH_ORD}                                                  AS ord,
        {_DEPTH_LABEL}                                                AS bucket,
        COUNT(*)                                                      AS hands,
        AVG(depth)                                                    AS avg_depth,
        AVG(vpip::INT)                                                AS vpip,
        AVG(pfr::INT)                                                 AS pfr,
        AVG(saw_flop::INT)                                            AS flop,
        SUM(pf_3bet::INT)::NUMERIC
            / NULLIF(SUM(pf_3bet_opp::INT), 0)                        AS three_bet,
        SUM(went_to_showdown::INT)::NUMERIC
            / NULLIF(SUM(saw_flop::INT), 0)                           AS wtsd,
        SUM(net_chips::NUMERIC / big_blind)                           AS net_bb
    FROM d
    GROUP BY 1, 2
    ORDER BY 1
"""

# Seats from the button round to the blinds; anything _calc_position couldn't
# name sorts last, since array_position returns NULL and NULLs sort last.
_POSITION_ORDER = "ARRAY['BTN','BTN/SB','CO','HJ','MP','UTG+2','UTG+1','UTG','SB','BB']"


def _query(conn, sql: str, params: dict) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def _one(conn, sql: str, params: dict) -> dict | None:
    rows = _query(conn, sql, params)
    return rows[0] if rows else None


# ── Which sessions need a workbook ────────────────────────────────────────────

def due_sessions(
    conn,
    *,
    gap_minutes: int = DEFAULT_GAP_MINUTES,
    lookback_days: int = 7,
    grace_hours: int = 6,
) -> list[dict]:
    """
    Sessions that are over and whose workbook is missing or out of date.

    Four conditions, each earning its place:
      - the last hand is older than the gap, which is what "over" means; no file
        event is involved, so a delayed sync postpones the export without ever
        distorting it
      - the session ended within `lookback_days`, so switching the watcher on
        against an existing database doesn't dump the entire history to disk —
        that is what the CLI's --rebuild-all is for
      - every tournament has its summary, or `grace_hours` have passed. Buy-in
        and winnings only arrive with the summary, so exporting early would
        report a wrong profit; the grace period keeps a summary that never
        lands from blocking the session forever
      - nothing was exported yet, or the counts moved since — a late hand or a
        late summary rewrites the file instead of leaving a stale one
    """
    return _query(
        conn,
        f"""
        WITH s AS ({_SESSION_AGG})
        SELECT s.*
        FROM s
        LEFT JOIN session_exports e USING (session_id)
        WHERE s.session_end <= (NOW() AT TIME ZONE 'UTC') - %(gap)s
          AND s.session_end >= (NOW() AT TIME ZONE 'UTC') - %(lookback)s
          AND (
                s.summaries_count = s.tournaments_count
                OR s.session_end <= (NOW() AT TIME ZONE 'UTC') - %(grace)s
              )
          AND (
                e.session_id IS NULL
                OR (e.tournaments_count, e.hands_count, e.summaries_count)
                   IS DISTINCT FROM (s.tournaments_count, s.hands_count, s.summaries_count)
              )
        ORDER BY s.session_start
        """,
        {
            "gap": timedelta(minutes=gap_minutes),
            "lookback": timedelta(days=lookback_days),
            "grace": timedelta(hours=grace_hours),
        },
    )


def list_sessions(conn, *, gap_minutes: int = DEFAULT_GAP_MINUTES, since=None) -> list[dict]:
    """Every session, oldest first — the CLI's --rebuild-all and --since."""
    return _query(
        conn,
        f"""
        WITH s AS ({_SESSION_AGG})
        SELECT * FROM s
        WHERE %(since)s::TIMESTAMP IS NULL OR session_start >= %(since)s::TIMESTAMP
        ORDER BY session_start
        """,
        {"gap": timedelta(minutes=gap_minutes), "since": since},
    )


def latest_session_id(conn, *, gap_minutes: int = DEFAULT_GAP_MINUTES) -> str | None:
    row = _one(
        conn,
        f"WITH s AS ({_SESSION_AGG}) SELECT session_id FROM s ORDER BY session_start DESC LIMIT 1",
        {"gap": timedelta(minutes=gap_minutes)},
    )
    return row["session_id"] if row else None


# ── Everything the workbook needs ─────────────────────────────────────────────

def fetch_session(conn, session_id: str, *, gap_minutes: int = DEFAULT_GAP_MINUTES) -> dict | None:
    """
    Collect one session into the dict the exporter renders.

    Returns None when the id matches nothing, which happens legitimately: the
    id encodes the local start time, so re-importing an earlier tournament that
    merges into the session moves its boundary and renames it.
    """
    params = {"gap": timedelta(minutes=gap_minutes), "sid": session_id}

    meta = _one(conn, f"WITH s AS ({_SESSION_AGG}) SELECT * FROM s WHERE session_id = %(sid)s", params)
    if meta is None:
        return None

    player_params = params | {"player": meta["player"], "end": meta["session_end"]}

    # Everything compared against stops at the end of the session — the rates,
    # the depth reference, the lifetime profit and the standard deviation. Two
    # reasons: comparing a night's play against a baseline that already
    # contains later hands answers the wrong question, and a workbook
    # regenerated months afterwards would otherwise come back with different
    # reference figures for a session that has not changed.
    lifetime_scope = f"hero = %(player)s AND hand_datetime <= %(end)s AND {_IN_MTT}"

    return {
        "meta": meta,
        "index": _session_index(conn, player_params),
        "lifetime_profit": _lifetime_profit(conn, player_params),
        "variance": _variance(conn, player_params),
        "rates": _one(conn, _RATES.format(scope=_SESSION_MTT_HANDS), params),
        "rates_lifetime": _one(conn, _RATES.format(scope=lifetime_scope), player_params),
        "formats": _formats(conn, params),
        "tournaments": _tournaments(conn, params),
        "exits": _exits(conn, params),
        "depths": _query(conn, _DEPTHS.format(scope=_SESSION_MTT_HANDS), params),
        "depths_lifetime": _query(conn, _DEPTHS.format(scope=lifetime_scope), player_params),
        "positions": _positions(conn, params),
        "best_hands": _extreme_hands(conn, params, "DESC"),
        "worst_hands": _extreme_hands(conn, params, "ASC"),
    }


def _session_index(conn, params: dict) -> int:
    """This session's rank in the player's history — 'session #42'."""
    row = _one(
        conn,
        """
        SELECT COUNT(DISTINCT session_id) AS n
        FROM f_session_map(%(gap)s)
        WHERE player = %(player)s AND session_start <= %(end)s
        """,
        params,
    )
    return row["n"] if row else 0


def _lifetime_profit(conn, params: dict) -> float:
    """Net profit over everything played up to the end of this session."""
    row = _one(
        conn,
        """
        SELECT ROUND(COALESCE(SUM(total_won), 0) - COALESCE(SUM(buyin_total), 0), 2) AS profit
        FROM tournaments
        WHERE player = %(player)s AND buyin_total IS NOT NULL AND started_at <= %(end)s
        """,
        params,
    )
    return float(row["profit"]) if row and row["profit"] is not None else 0.0


def _variance(conn, params: dict) -> dict:
    """
    How much noise a session of this size carries, measured on the player's own
    results rather than on a textbook number.

    The per-tournament profit in buy-ins is the unit: it is what makes a 2 €
    and a 20 € tournament comparable, and its standard deviation is the whole
    reason a session's profit says so little. Freerolls and ticket entries are
    excluded — a buy-in of zero has no ROI to speak of.

    Every format counts here, Expresso included, deliberately: this figure is
    weighed against the session's total profit, and both sides have to cover
    the same population or the comparison means nothing.
    """
    row = _one(
        conn,
        """
        SELECT
            COUNT(*)                                                      AS sample,
            STDDEV_SAMP((total_won - buyin_total) / buyin_total)           AS sd,
            AVG((total_won - buyin_total) / buyin_total)                   AS roi
        FROM tournaments
        WHERE player = %(player)s AND buyin_total > 0 AND started_at <= %(end)s
        """,
        params,
    )
    sample = row["sample"] if row else 0
    sd = float(row["sd"]) if row and row["sd"] is not None else None
    enough = sample >= MIN_VARIANCE_SAMPLE and sd is not None and sd > 0
    return {
        "sample": sample,
        "sd_buyins": sd if enough else DEFAULT_SD_BUYINS,
        "roi": float(row["roi"]) if row and row["roi"] is not None else None,
        "borrowed": not enough,
    }


def _formats(conn, params: dict) -> list[dict]:
    """
    One row per format played, with counters rather than finished rates: the
    workbook adds a "toutes formes" column, and summing rates would be wrong
    where summing numerators and denominators is right.
    """
    return _query(
        conn,
        f"""
        WITH s AS ({_SESSION_TOURNAMENTS}),
             hc AS ({_SESSION_HAND_COUNTS.format(scope=_IN_SESSION)})
        SELECT
            {_FAMILY}::TEXT                                           AS family,
            COUNT(*)                                                  AS tournaments,
            -- Entries that actually cost something; see avg_buyin above.
            COUNT(*) FILTER (WHERE t.buyin_total > 0)                 AS paid,
            SUM(COALESCE(t.entries, 1) - 1)                           AS reentries,
            COUNT(*) FILTER (WHERE t.prize_won > 0)                   AS itm,
            COUNT(*) FILTER (WHERE t.total_won > 0)                   AS cashed,
            COUNT(*) FILTER (WHERE t.bounty_won > 0)                  AS bountied,
            COUNT(*) FILTER (WHERE t.finish_position <= {FINAL_TABLE_PLACES})
                                                                      AS final_tables,
            COUNT(*) FILTER (
                WHERE t.finish_position::NUMERIC
                      / NULLIF(t.players_registered, 0) <= {DEEP_RUN_PCT}
            )                                                         AS deep_runs,
            PERCENTILE_CONT(0.5) WITHIN GROUP (
                ORDER BY t.finish_position::NUMERIC / NULLIF(t.players_registered, 0)
            )                                                         AS median_finish_pct,
            SUM(t.players_registered)                                 AS field_sum,
            COUNT(t.players_registered)                               AS field_n,
            SUM(t.buyin_total)                                        AS invested,
            SUM(t.buyin_prize)                                        AS prize_invested,
            SUM(t.buyin_bounty)                                       AS bounty_invested,
            SUM(t.buyin_rake)                                         AS rake,
            SUM(t.total_won)                                          AS won,
            SUM(t.prize_won)                                          AS prize_won,
            SUM(t.bounty_won)                                         AS bounty_won,
            SUM(t.duration_seconds)                                   AS played_seconds,
            COALESCE(SUM(hc.hands), 0)::INT                           AS hands,
            COALESCE(SUM(hc.vpip), 0)::INT                            AS vpip_hands,
            COALESCE(SUM(hc.pfr), 0)::INT                             AS pfr_hands,
            COALESCE(SUM(hc.flop), 0)::INT                            AS flop_hands,
            COALESCE(SUM(hc.three_bet), 0)::INT                       AS three_bet,
            COALESCE(SUM(hc.three_bet_opp), 0)::INT                   AS three_bet_opp
        FROM tournaments t
        JOIN s USING (tournament_id)
        LEFT JOIN hc USING (tournament_id)
        GROUP BY 1
        -- The expression is repeated rather than sorted on the output name:
        -- an output name only stands alone in ORDER BY, never inside a call.
        ORDER BY ARRAY_POSITION(ARRAY{list(FAMILIES)}::TEXT[], {_FAMILY}::TEXT)
        """,
        params,
    )


def _tournaments(conn, params: dict) -> list[dict]:
    """
    Ordered by when the hero left the table, not when they sat down: that is the
    order the money actually moved, and therefore the order of the profit curve.
    """
    return _query(
        conn,
        f"""
        SELECT
            t.tournament_id, t.name, t.started_at, t.type, t.speed,
            {_FAMILY}::TEXT          AS family,
            t.buyin_total, t.buyin_bounty, t.buyin_rake, t.entries,
            t.players_registered, t.finish_position,
            t.prize_won, t.bounty_won, t.total_won, t.roi, t.duration_seconds,
            COUNT(h.hand_id)         AS hands,
            MAX(h.hand_datetime)     AS last_hand,
            AVG(h.vpip::INT)         AS vpip,
            AVG(h.pfr::INT)          AS pfr,
            AVG(h.saw_flop::INT)     AS flop,
            -- The lateral returns one row per tournament, so MAX is only there
            -- to satisfy the grouping.
            MAX(x.exit_bb)           AS exit_bb
        FROM tournaments t
        LEFT JOIN hands h USING (tournament_id)
        LEFT JOIN LATERAL (
            SELECT ROUND(h2.starting_chips::NUMERIC / NULLIF(h2.big_blind, 0), 1) AS exit_bb
            FROM hands h2
            WHERE h2.tournament_id = t.tournament_id
            ORDER BY h2.hand_datetime DESC, h2.hand_id DESC
            LIMIT 1
        ) x ON TRUE
        WHERE t.{_IN_SESSION}
        GROUP BY t.tournament_id
        ORDER BY COALESCE(MAX(h.hand_datetime), t.started_at)
        """,
        params,
    )


def _exits(conn, params: dict) -> list[dict]:
    """
    How each tournament ended: the last hand played, and the stack the hero
    carried into it.

    `starting_chips` on that hand is the depth at the moment of the decision
    that ended the tournament — the figure that separates "the stack played
    itself" from "a pot was chosen and lost". Deepest exits first, which is the
    order they deserve to be read in. A tournament won outright still has a
    last hand; `won_tournament` marks it so it can be kept out of the medians.
    """
    return _query(
        conn,
        f"""
        WITH s AS ({_SESSION_TOURNAMENTS})
        SELECT
            t.tournament_id,
            t.name,
            {_FAMILY}::TEXT                                           AS family,
            t.finish_position,
            t.players_registered,
            t.finish_position::NUMERIC
                / NULLIF(t.players_registered, 0)                     AS finish_pct,
            t.total_won,
            t.finish_position = 1                                     AS won_tournament,
            x.hand_datetime, x.level, x.big_blind, x.starting_chips,
            x.hero_position, x.hero_cards, x.went_to_showdown,
            ROUND(x.starting_chips::NUMERIC / NULLIF(x.big_blind, 0), 1) AS exit_bb,
            ROUND(x.net_chips::NUMERIC / NULLIF(x.big_blind, 0), 1)      AS net_bb
        FROM tournaments t
        JOIN s USING (tournament_id)
        LEFT JOIN LATERAL (
            SELECT h2.hand_datetime, h2.level, h2.big_blind, h2.starting_chips,
                   h2.hero_position, h2.hero_cards, h2.went_to_showdown, h2.net_chips
            FROM hands h2
            WHERE h2.tournament_id = t.tournament_id
            ORDER BY h2.hand_datetime DESC, h2.hand_id DESC
            LIMIT 1
        ) x ON TRUE
        ORDER BY COALESCE(t.finish_position = 1, FALSE), exit_bb DESC NULLS LAST
        """,
        params,
    )


def _positions(conn, params: dict) -> list[dict]:
    return _query(
        conn,
        f"""
        SELECT
            hero_position            AS position,
            COUNT(*)                 AS hands,
            AVG(vpip::INT)           AS vpip,
            AVG(pfr::INT)            AS pfr,
            AVG(saw_flop::INT)       AS flop,
            SUM(net_chips)           AS net_chips,
            -- Chips are worthless across levels; big blinds are the unit that
            -- makes level 3 and level 22 comparable.
            ROUND(SUM(net_chips::NUMERIC / NULLIF(big_blind, 0)), 1) AS net_bb
        FROM hands
        WHERE {_SESSION_MTT_HANDS} AND hero_position IS NOT NULL
        GROUP BY hero_position
        ORDER BY ARRAY_POSITION({_POSITION_ORDER}, hero_position)
        """,
        params,
    )


def _extreme_hands(conn, params: dict, direction: str) -> list[dict]:
    """The ten biggest pots won or lost, in big blinds. `direction` is a literal."""
    return _query(
        conn,
        f"""
        SELECT
            hand_datetime, tournament_id, level, hero_position, hero_cards,
            big_blind, net_chips, saw_flop, went_to_showdown, won_hand,
            ROUND(starting_chips::NUMERIC / NULLIF(big_blind, 0), 1) AS depth_bb,
            ROUND(net_chips::NUMERIC / NULLIF(big_blind, 0), 1)      AS net_bb
        FROM hands
        WHERE {_SESSION_MTT_HANDS} AND big_blind > 0 AND net_chips <> 0
        ORDER BY net_chips::NUMERIC / NULLIF(big_blind, 0) {direction}
        LIMIT 10
        """,
        params,
    )


# ── Export bookkeeping ────────────────────────────────────────────────────────

def record_export(conn, meta: dict, filepath: str) -> None:
    """Store the content fingerprint so the file is only rewritten when it moves."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO session_exports (
                session_id, filepath, tournaments_count, hands_count,
                summaries_count, exported_at
            ) VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (session_id) DO UPDATE SET
                filepath          = EXCLUDED.filepath,
                tournaments_count = EXCLUDED.tournaments_count,
                hands_count       = EXCLUDED.hands_count,
                summaries_count   = EXCLUDED.summaries_count,
                exported_at       = EXCLUDED.exported_at
            """,
            (
                meta["session_id"],
                filepath,
                meta["tournaments_count"],
                meta["hands_count"],
                meta["summaries_count"],
            ),
        )
