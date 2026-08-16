"""
Winamax file watchdog — monitors a directory and imports tournament files into PostgreSQL.

Two file types per tournament are handled:
  *_summary.txt              → tournaments table  (result, ROI, duration…)
  *_holdem_no-limit.txt      → hands table        (VPIP, PFR, position, chips…)

A file is re-imported whenever its size or mtime changed since the last run
(imported_files), so a tournament still in progress keeps being topped up as
Winamax appends hands to it. Inserts are idempotent, so replaying a file only
adds what is new.

The summary and history can arrive in any order; history creates a lightweight
tournament placeholder row that the summary later fills in via UPSERT.

Once a session has gone quiet it is exported: a workbook per session in
EXPORT_DIR, and a row appended to the bankroll spreadsheet kept beside them
(BANKROLL_FILE). Both go through exporter.py, on a timer rather than on file
events — a batch of files landing an hour late must delay an export, never
split a session in two.
"""

import time
import psycopg2
from psycopg2.extras import execute_values
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import exporter
import sessions
from parser import (
    PARSER_VERSION,
    is_summary_file,
    is_winamax_file,
    parse_summary,
    parse_history,
)

# ── Config ────────────────────────────────────────────────────────────────────

import os

DB_DSN         = os.environ["DB_URL"]              # postgresql://user:pass@postgres:5432/db
WATCH_DIR      = os.environ["WATCH_DIR"]
WINAMAX_PSEUDO = os.environ.get("WINAMAX_PSEUDO")  # override l'auto-détection du hero
SCHEMA_PATH    = os.environ.get(
    "SCHEMA_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
)

# Session workbooks. Leaving EXPORT_DIR unset turns the feature off entirely;
# the CLI (python exporter.py) still works with --out.
EXPORT_DIR           = os.environ.get("EXPORT_DIR")
SESSION_GAP_MINUTES  = int(os.environ.get("SESSION_GAP_MINUTES", sessions.DEFAULT_GAP_MINUTES))
EXPORT_LOOKBACK_DAYS = int(os.environ.get("EXPORT_LOOKBACK_DAYS", 7))
EXPORT_GRACE_HOURS   = int(os.environ.get("EXPORT_GRACE_HOURS", 6))
EXPORT_CHECK_SECONDS = int(os.environ.get("EXPORT_CHECK_SECONDS", 300))

# The bankroll spreadsheet, kept in the export folder alongside the workbooks.
# BANKROLL_PLAYER matters as much as the path: the watch directory holds the
# backups of several machines and more than one Winamax account, and a bankroll
# that added two accounts together would describe neither of them.
BANKROLL_FILE   = os.environ.get("BANKROLL_FILE") or (
    os.path.join(EXPORT_DIR, "bankroll_poker_mtt.ods") if EXPORT_DIR else None
)
BANKROLL_PLAYER = os.environ.get("BANKROLL_PLAYER") or None

# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(DB_DSN)


def init_schema():
    """
    Apply schema.sql — the single source of truth for tables, indexes and views.

    Postgres only runs docker-entrypoint-initdb.d on a *fresh* volume, so a
    schema change would otherwise never reach an existing database. Re-applying
    it here on every start keeps the two in sync; schema.sql is idempotent.
    """
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema_sql = f.read()

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(schema_sql)
    conn.commit()
    conn.close()
    print(f"[DB] Schema applied from {SCHEMA_PATH}")


# ── Watchdog handler ──────────────────────────────────────────────────────────

class WinamaxHandler(FileSystemEventHandler):
    def __init__(self):
        super().__init__()
        # filepath → (size, mtime) last handed to the DB, to swallow the burst
        # of duplicate events watchdog emits for a single write.
        self._seen: dict[str, tuple[int, float]] = {}

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".txt"):
            self._process(event.src_path)

    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(".txt"):
            self._process(event.src_path)

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def _process(self, filepath: str):
        try:
            st = os.stat(filepath)
        except OSError:
            return  # vanished between the event and now
        fingerprint = (st.st_size, st.st_mtime)

        if self._seen.get(filepath) == fingerprint:
            return

        # A watch directory pointed at a machine backup holds thousands of .txt
        # that have nothing to do with poker — range charts, notes, exports.
        # Checking the header costs one short read and keeps them out of both
        # the import path and the imported_files table.
        if not is_winamax_file(filepath):
            self._seen[filepath] = fingerprint
            return

        conn = get_db()
        try:
            # Already imported *in this exact state, by this parser*? Anything
            # else — a new file, one Winamax has appended to, or one read by an
            # older parser — is (re)processed. That last case is what backfills
            # columns a parser upgrade introduced: bumping PARSER_VERSION makes
            # every stored file compare unequal exactly once.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT file_size, file_mtime, parser_version"
                    " FROM imported_files WHERE filepath = %s",
                    (filepath,),
                )
                row = cur.fetchone()
            if row and (row[0], row[1]) == fingerprint and row[2] == PARSER_VERSION:
                self._seen[filepath] = fingerprint
                return

            if is_summary_file(filepath):
                self._import_summary(conn, filepath)
                ftype, detail = "summary", ""
            else:
                new, refreshed = self._import_history(conn, filepath)
                ftype = "history"
                detail = f" (+{new} hands)" + (f", {refreshed} refreshed" if refreshed else "")

            # Record the imported state only after a successful commit
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO imported_files (
                        filepath, file_type, file_size, file_mtime, parser_version, imported_at
                    ) VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (filepath) DO UPDATE SET
                        file_type      = EXCLUDED.file_type,
                        file_size      = EXCLUDED.file_size,
                        file_mtime     = EXCLUDED.file_mtime,
                        parser_version = EXCLUDED.parser_version,
                        imported_at    = EXCLUDED.imported_at
                    """,
                    (filepath, ftype, st.st_size, st.st_mtime, PARSER_VERSION),
                )
            conn.commit()
            self._seen[filepath] = fingerprint
            print(f"[OK:{ftype}]{detail} {filepath}")

        except Exception as exc:
            conn.rollback()
            print(f"[ERR] {filepath}: {exc}")
        finally:
            conn.close()

    # ── Summary importer ──────────────────────────────────────────────────────

    def _import_summary(self, conn, filepath: str):
        d = parse_summary(filepath)
        if not d["tournament_id"]:
            raise ValueError("aucun ID de tournoi trouvé (nom de fichier ni contenu)")
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tournaments (
                    tournament_id, name, player, started_at,
                    buyin_prize, buyin_bounty, buyin_rake, buyin_total,
                    players_registered, mode, type, speed,
                    prizepool, entries, duration_seconds, finish_position,
                    prize_won, bounty_won, total_won, roi
                ) VALUES (
                    %(tournament_id)s, %(name)s, %(player)s, %(started_at)s,
                    %(buyin_prize)s, %(buyin_bounty)s, %(buyin_rake)s, %(buyin_total)s,
                    %(players_registered)s, %(mode)s, %(type)s, %(speed)s,
                    %(prizepool)s, %(entries)s, %(duration_seconds)s, %(finish_position)s,
                    %(prize_won)s, %(bounty_won)s, %(total_won)s, %(roi)s
                )
                ON CONFLICT (tournament_id) DO UPDATE SET
                    name               = EXCLUDED.name,
                    player             = EXCLUDED.player,
                    started_at         = EXCLUDED.started_at,
                    buyin_prize        = EXCLUDED.buyin_prize,
                    buyin_bounty       = EXCLUDED.buyin_bounty,
                    buyin_rake         = EXCLUDED.buyin_rake,
                    buyin_total        = EXCLUDED.buyin_total,
                    players_registered = EXCLUDED.players_registered,
                    mode               = EXCLUDED.mode,
                    type               = EXCLUDED.type,
                    speed              = EXCLUDED.speed,
                    prizepool          = EXCLUDED.prizepool,
                    entries            = EXCLUDED.entries,
                    duration_seconds   = EXCLUDED.duration_seconds,
                    finish_position    = EXCLUDED.finish_position,
                    prize_won          = EXCLUDED.prize_won,
                    bounty_won         = EXCLUDED.bounty_won,
                    total_won          = EXCLUDED.total_won,
                    roi                = EXCLUDED.roi
                -- The watch directory holds backups of several machines, so the
                -- same tournament arrives more than once, sometimes captured at
                -- different moments. Walk order is arbitrary, so without this
                -- guard a snapshot taken before a re-entry could overwrite the
                -- complete one. Entry count only ever grows.
                WHERE EXCLUDED.entries >= COALESCE(tournaments.entries, 1)
                """,
                d,
            )

    # ── History importer ──────────────────────────────────────────────────────

    def _import_history(self, conn, filepath: str) -> tuple[int, int]:
        """
        Insert every complete hand in the file; returns (new, refreshed).

        Every column is re-computed on conflict rather than skipped, so a parser
        fix reaches hands that were already stored: re-importing the file is
        enough to correct them. Rewriting the whole row rather than a chosen few
        columns is what let version 2 add its stats and correct saw_flop without
        anyone having to remember to extend a list here.
        """
        hands, hero = parse_history(filepath, hero_override=WINAMAX_PSEUDO)
        if not hands:
            return 0, 0

        tid = hands[0]["tournament_id"]
        if not tid:
            raise ValueError("aucun ID de tournoi trouvé (nom de fichier ni contenu)")

        # Ensure the parent tournament row exists before the FK insert.
        # If the summary hasn't been processed yet this creates a minimal
        # placeholder that the summary UPSERT will later fill in.
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tournaments (tournament_id, player) VALUES (%s, %s)"
                " ON CONFLICT (tournament_id) DO NOTHING",
                (tid, hero),
            )

        cols = [
            "hand_id", "tournament_id", "table_name",
            "level", "ante", "small_blind", "big_blind", "hand_datetime",
            "hero", "hero_seat", "hero_position", "hero_cards",
            "starting_chips", "saw_flop", "went_to_showdown", "won_hand",
            "chips_won", "chips_invested", "net_chips", "vpip", "pfr",
            "pf_3bet_opp", "pf_3bet", "faced_3bet", "folded_to_3bet",
            "cbet_flop_opp", "cbet_flop",
            "postflop_bets", "postflop_raises", "postflop_calls", "postflop_folds",
        ]
        updates = ",\n                    ".join(
            f"{c} = EXCLUDED.{c}" for c in cols if c != "hand_id"
        )
        # DO UPDATE refuses to touch the same row twice in one statement, so a
        # hand repeated inside the file would abort the batch. Keep the last copy.
        rows = [tuple(h[c] for c in cols) for h in {h["hand_id"]: h for h in hands}.values()]

        with conn.cursor() as cur:
            # xmax = 0 on a row the statement inserted, non-zero on one it updated.
            returned = execute_values(
                cur,
                f"""
                INSERT INTO hands ({', '.join(cols)})
                VALUES %s
                ON CONFLICT (hand_id) DO UPDATE SET
                    {updates}
                RETURNING (xmax = 0)
                """,
                rows,
                fetch=True,
            )

        new = sum(1 for (is_insert,) in returned)
        return new, len(returned) - new


# ── Backfill ──────────────────────────────────────────────────────────────────

def backfill(handler: WinamaxHandler, watch_dir: str):
    """
    Process all .txt files already present in watch_dir at startup.
    History files are processed first so the tournament placeholder rows exist
    before the summary UPSERT runs.
    """
    all_txt = [
        os.path.join(root, fname)
        for root, _, files in os.walk(watch_dir)
        for fname in files
        if fname.endswith(".txt")
    ]
    history_files = [f for f in all_txt if not is_summary_file(f)]
    summary_files = [f for f in all_txt if     is_summary_file(f)]

    total = len(history_files) + len(summary_files)
    print(f"[Backfill] {total} files  ({len(history_files)} history, {len(summary_files)} summary)")

    for filepath in history_files + summary_files:
        handler._process(filepath)

    print("[Backfill] Done.")


# ── Session export ────────────────────────────────────────────────────────────

def export_tick():
    """
    Write a workbook for every session that has gone quiet.

    The decision is entirely data-driven — it compares hand timestamps to the
    clock, never file events. A batch of files landing an hour after the fact,
    which is what a NAS sync looks like, therefore delays the export without
    ever moving a session boundary or distorting a figure.

    Failures are logged and swallowed: an export that goes wrong must not take
    the importer down with it, and the next tick retries anyway since nothing
    was recorded in session_exports.
    """
    conn = get_db()
    try:
        exporter.export_due(
            conn,
            EXPORT_DIR,
            gap_minutes=SESSION_GAP_MINUTES,
            lookback_days=EXPORT_LOOKBACK_DAYS,
            grace_hours=EXPORT_GRACE_HOURS,
            ledger=BANKROLL_FILE,
            ledger_player=BANKROLL_PLAYER,
        )
    except Exception as exc:
        conn.rollback()
        print(f"[ERR:export] {exc}")
    finally:
        conn.close()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_schema()

    handler = WinamaxHandler()
    backfill(handler, WATCH_DIR)   # ingest existing files first

    observer = Observer()
    observer.schedule(handler, path=WATCH_DIR, recursive=True)
    observer.start()
    print(f"[Watching] {WATCH_DIR}")
    if EXPORT_DIR:
        print(f"[Export] {EXPORT_DIR} — coupure de session à {SESSION_GAP_MINUTES} min")
    else:
        print("[Export] désactivé (EXPORT_DIR non défini)")

    # Said out loud at startup: a bankroll silently not updated for weeks is
    # worse than one that was never switched on.
    if not EXPORT_DIR:
        pass
    elif not os.path.exists(BANKROLL_FILE):
        print(f"[Bankroll] fichier introuvable : {BANKROLL_FILE} — bankroll non tenue")
    elif BANKROLL_PLAYER:
        print(f"[Bankroll] {BANKROLL_FILE} — sessions de {BANKROLL_PLAYER}")
    else:
        print(f"[Bankroll] {BANKROLL_FILE} — tous les joueurs "
              "(définir BANKROLL_PLAYER pour n'en suivre qu'un)")

    next_export = 0.0   # 0 so the first check runs right after the backfill
    try:
        while True:
            time.sleep(1)
            if EXPORT_DIR and time.monotonic() >= next_export:
                next_export = time.monotonic() + EXPORT_CHECK_SECONDS
                export_tick()
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
