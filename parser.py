"""
Winamax file parser — handles both file types produced per tournament:
  - *_summary.txt          → tournament metadata (result, buy-in, ROI…)
  - *_holdem_no-limit.txt  → hand-by-hand history (VPIP, PFR, position…)
"""

import os
import re
from datetime import datetime


# Bumped whenever a change here alters the values produced for an unchanged
# file. The watcher stores it alongside each import and re-reads any file whose
# recorded version is older, which is what backfills new columns and propagates
# fixes to hands already in the database.
#   1 → initial release
#   2 → street-by-street action log: 3-bet, fold-to-3-bet, C-bet, aggression
#       counters, and a corrected saw_flop / went_to_showdown (see _parse_hand)
#   3 → real-file corrections: tournament ID read from the parentheses Winamax
#       actually uses, player names containing spaces, summaries holding one
#       block per re-entry, optional "You won" / "You played" lines, headers
#       with no ante, dead buttons, and split pots ("main pot" / "side pot N")
#   4 → knockout bounties collected without cashing: a "You won" line carrying
#       a bounty and no prize was matched by nothing and the entry stored as a
#       plain bust (see _parse_winnings)
PARSER_VERSION = 4

# Every Winamax file starts with this, whatever the game or the language. Used
# to tell a hand history from the thousands of unrelated .txt a backup folder
# happens to contain.
FILE_SIGNATURE = "Winamax Poker - Tournament"


# ── Helpers ──────────────────────────────────────────────────────────────────

def is_summary_file(filepath: str) -> bool:
    return filepath.endswith("_summary.txt")


def is_winamax_file(filepath: str) -> bool:
    """True when the file actually opens with a Winamax header."""
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            return f.read(len(FILE_SIGNATURE)) == FILE_SIGNATURE
    except OSError:
        return False


# Winamax writes the tournament ID in parentheses right after the name, both in
# the filename and in the file:
#   20251206_STARDUST - SPACE KO(1014872879)_real_holdem_no-limit.txt
# The name itself carries digits, spaces, '#' and '-', so the parentheses are
# the only dependable anchor. The underscore form is kept as a fallback for
# files renamed by other tools.
_ID_IN_PARENS = re.compile(r"\((\d{6,12})\)")
_ID_BETWEEN_UNDERSCORES = re.compile(r"_(\d{7,12})_")
_ID_IN_CONTENT = re.compile(
    r"(?:Table: '|Tournament summary : )[^\n']*?\((\d{6,12})\)"
)


def extract_tournament_id(filepath: str) -> str | None:
    """Pull the numeric tournament ID from the filename."""
    name = os.path.basename(filepath)
    for pattern in (_ID_IN_PARENS, _ID_BETWEEN_UNDERSCORES):
        if (m := pattern.search(name)) is not None:
            return m.group(1)
    return None


def _id_from_content(text: str) -> str | None:
    """Fallback for a renamed file: the ID also sits in the table name and in
    the summary header."""
    m = _ID_IN_CONTENT.search(text)
    return m.group(1) if m else None


def _get(pattern: str, text: str, cast=str, group: int = 1):
    m = re.search(pattern, text)
    return cast(m.group(group)) if m else None


# ── Summary parser ────────────────────────────────────────────────────────────

# Every key the tournaments INSERT names. Missing lines must come back as None
# rather than as an absent key: psycopg2 resolves %(name)s against the dict and
# raises KeyError on the first one it cannot find, which aborts the whole file.
_SUMMARY_KEYS = (
    "tournament_id", "name", "player", "started_at",
    "buyin_prize", "buyin_bounty", "buyin_rake", "buyin_total",
    "players_registered", "mode", "type", "speed", "prizepool",
    "entries", "duration_seconds", "finish_position",
    "prize_won", "bounty_won", "total_won", "roi",
)


def parse_summary(filepath: str) -> dict:
    """
    Returns a flat dict ready for INSERT into the `tournaments` table.

    One file can hold several summary blocks — Winamax appends one per entry,
    so a re-entry tournament ends up with two or more. They are folded into a
    single row: money and time add up across entries, while the field, the
    prize pool and the finishing place come from the last block, the only one
    describing how the tournament actually ended for this player.
    """
    with open(filepath, encoding="utf-8") as f:
        txt = f.read()

    blocks = [b for b in re.split(r"(?=Winamax Poker - Tournament summary :)", txt) if b.strip()]
    parsed = [_parse_summary_block(b) for b in blocks] or [_parse_summary_block(txt)]
    last = parsed[-1]

    d = dict.fromkeys(_SUMMARY_KEYS)
    d.update(last)
    d["tournament_id"] = extract_tournament_id(filepath) or _id_from_content(txt)
    d["entries"] = len(parsed)

    # Each entry costs its own buy-in, and the player sat down for each of them.
    if last["buyin_total"] is not None:
        for field in ("buyin_prize", "buyin_bounty", "buyin_rake", "buyin_total"):
            d[field] = round(last[field] * len(parsed), 2)

    durations = [p["duration_seconds"] for p in parsed if p["duration_seconds"] is not None]
    d["duration_seconds"] = sum(durations) if durations else None

    # A block with no "You won" line is a bust, which is 0 collected, not
    # unknown — the distinction is what makes a -100% ROI come out right.
    d["prize_won"] = round(sum(p["prize_won"] for p in parsed), 2)
    d["bounty_won"] = round(sum(p["bounty_won"] for p in parsed), 2)
    d["total_won"] = round(d["prize_won"] + d["bounty_won"], 2)

    if d["buyin_total"]:
        d["roi"] = round((d["total_won"] - d["buyin_total"]) / d["buyin_total"] * 100, 2)

    return d


def _parse_summary_block(txt: str) -> dict:
    """One "Tournament summary" block — a single entry into the tournament."""
    d = dict.fromkeys(_SUMMARY_KEYS)

    # Name  (everything between "summary : " and the opening paren / newline)
    m = re.search(r"Tournament summary : (.+?)(?:\(|\n)", txt)
    d["name"] = m.group(1).strip() if m else None

    d["player"] = _get(r"Player : (\S+)", txt)
    d["players_registered"] = _get(r"Registered players : (\d+)", txt, int)
    d["mode"] = _get(r"Mode : (\S+)", txt)
    d["type"] = _get(r"Type : (\S+)", txt)
    d["speed"] = _get(r"Speed : (\S+)", txt)
    d["prizepool"] = _get(r"Prizepool : ([\d.]+)€", txt, float)
    d["finish_position"] = _get(r"You finished in (\d+)(?:st|nd|rd|th) place", txt, int)

    # Buy-in breakdown. Three amounts on a knockout (prize + bounty + rake),
    # two on a regular tournament (prize + rake), none at all on a freeroll or
    # a ticket entry — where zero is the honest answer, not unknown.
    m = re.search(r"Buy-In : ([^\n]+)", txt)
    amounts = [float(x) for x in re.findall(r"([\d.]+)€", m.group(1))] if m else []
    if len(amounts) == 3:
        d["buyin_prize"], d["buyin_bounty"], d["buyin_rake"] = amounts
    elif len(amounts) == 2:
        d["buyin_prize"], d["buyin_rake"] = amounts
        d["buyin_bounty"] = 0.0
    elif len(amounts) == 1:
        d["buyin_prize"], d["buyin_bounty"], d["buyin_rake"] = amounts[0], 0.0, 0.0
    elif m:
        d["buyin_prize"] = d["buyin_bounty"] = d["buyin_rake"] = 0.0
    if d["buyin_prize"] is not None:
        d["buyin_total"] = round(d["buyin_prize"] + d["buyin_bounty"] + d["buyin_rake"], 2)

    # Tournament start timestamp
    m = re.search(r"Tournament started (\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) UTC", txt)
    d["started_at"] = datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S") if m else None

    # Duration. Any of the three units can be missing: a quick bust prints
    # "You played 25min 21s", and the hours-minutes-seconds form was the only
    # one the first version recognised.
    m = re.search(r"You played (?:(\d+)h ?)?(?:(\d+)min ?)?(?:(\d+)s)?", txt)
    if m and any(m.groups()):
        h, mi, s = (int(g) if g else 0 for g in m.groups())
        d["duration_seconds"] = h * 3600 + mi * 60 + s

    d["prize_won"], d["bounty_won"] = _parse_winnings(txt)
    d["total_won"] = round(d["prize_won"] + d["bounty_won"], 2)

    return d


# One euro amount, in either notation — the client writes "1€", "6.15€" and,
# depending on its language, "6,15€".
_AMOUNT = r"(\d+(?:[.,]\d+)?)\s*€"
_WON_LINE = re.compile(r"You won ([^\n]+)")
# "Bounty", "Bounties", "Bounty :" — the word is the anchor, not its position.
_BOUNTY = re.compile(r"Bount(?:y|ies)\s*:?\s*" + _AMOUNT, re.IGNORECASE)
_BOUNTY_LINE = re.compile(r"^\s*Bount(?:y|ies)[^\n:€]*:?\s*" + _AMOUNT, re.IGNORECASE | re.MULTILINE)
_AMOUNT_RE = re.compile(_AMOUNT)


def _amount(s: str) -> float:
    return float(s.replace(",", "."))


def _parse_winnings(txt: str) -> tuple[float, float]:
    """
    (prize_won, bounty_won) for one entry, in €.

    Winamax prints a single line whose shape depends on what the entry brought
    back, and the prize half is not always there:
        "You won 6.15€ + Bounty 5.72€"   cashed in a knockout
        "You won 6.15€"                  cashed, no bounties
        "You won Bounty 1€"              bounties only — busted out of the
                                         money in a knockout, which the older
                                         "You won <amount>€ …" pattern could
                                         not match at all: the whole line was
                                         ignored and the entry booked as a
                                         plain bust, primes included.
    Nothing at all is printed when the entry brought back nothing, and that is
    a genuine 0, not an unknown — it is what makes a -100% ROI come out right.

    The bounty is read from its own keyword rather than from its position, so
    the prize is whatever amount is left in front of it.
    """
    m = _WON_LINE.search(txt)
    if m is None:
        # Some builds put the bounty on a line of its own rather than inside
        # the "You won" one; a knockout bust then has no "You won" at all.
        b = _BOUNTY_LINE.search(txt)
        return (0.0, _amount(b.group(1))) if b else (0.0, 0.0)

    won = m.group(1)
    bounty = 0.0
    if (b := _BOUNTY.search(won)) is not None:
        bounty = _amount(b.group(1))
        won = won[: b.start()]

    p = _AMOUNT_RE.search(won)
    return (_amount(p.group(1)) if p else 0.0), bounty


# ── History parser ────────────────────────────────────────────────────────────

def detect_hero(content: str) -> str | None:
    """
    The hero is whoever receives hole cards (always the same player in one file).

    Anchored on the bracket rather than on whitespace: Winamax pseudonyms may
    contain spaces — "Q d-chouette" sits at a table in these files — and
    stopping at the first space would name the hero "Q".
    """
    m = re.search(r"Dealt to (.+?) \[", content)
    return m.group(1) if m else None


def parse_history(filepath: str, hero_override: str | None = None) -> tuple[list[dict], str | None]:
    """
    Returns (hands, hero) where:
      - hands  : list of dicts, one per *complete* hand, ready for INSERT into
                 `hands` — a hand still being written is skipped and picked up
                 on a later pass
      - hero   : player name (for seeding the tournaments placeholder row)
    hero_override : si fourni (via WINAMAX_PSEUDO), court-circuite l'auto-détection.
    """
    with open(filepath, encoding="utf-8") as f:
        content = f.read()

    tid = extract_tournament_id(filepath) or _id_from_content(content)

    # The override only wins where it is credible. A watch directory holding
    # backups of several machines carries files from more than one account, and
    # forcing one pseudonym onto all of them would drop every hand of the other.
    detected = detect_hero(content)
    hero = hero_override if hero_override and f"Dealt to {hero_override} [" in content else detected

    blocks = re.split(r'(?=Winamax Poker - Tournament ")', content)
    hands = [
        h for block in blocks
        if (h := _parse_hand(block.strip(), tid, hero)) is not None
    ]

    return hands, hero


def _parse_hand(block: str, tid: str | None, hero: str | None) -> dict | None:
    # ── Header ──────────────────────────────────────────────────────────────
    # "level: 31 - HandId: #xxx - Holdem no limit (ante/SB/BB) - YYYY/MM/DD HH:MM:SS UTC"
    # The ante is printed only once there is one. Early Expresso and Sit&Go
    # levels have none and read "(10/20)", which the three-number form rejected
    # outright — the whole file then parsed to zero hands, silently.
    hm = re.search(
        r"level: (\d+) - HandId: #([\d-]+) - Holdem no limit \((?:(\d+)/)?(\d+)/(\d+)\)"
        r" - (\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) UTC",
        block,
    )
    if not hm:
        return None

    # Files are read while Winamax is still appending, so the last block can be
    # half-written. Every field below is filled before the SUMMARY section is
    # emitted, so its presence marks the hand as complete and safe to store.
    # The importer now refreshes the chip columns on re-import, so a truncated
    # hand would eventually be corrected — but skipping it keeps the wrong
    # figures out of the dashboards in the meantime.
    if "*** SUMMARY ***" not in block:
        return None

    level = int(hm.group(1))
    hand_id = hm.group(2)
    ante = int(hm.group(3)) if hm.group(3) else 0
    small_blind = int(hm.group(4))
    big_blind = int(hm.group(5))
    hand_datetime = datetime.strptime(hm.group(6), "%Y/%m/%d %H:%M:%S")

    # ── Table & button ──────────────────────────────────────────────────────
    tm = re.search(r"Table: '(.+?)'", block)
    table_name = tm.group(1) if tm else None

    bm = re.search(r"Seat #(\d+) is the button", block)
    btn_seat = int(bm.group(1)) if bm else None

    # ── Seats (header section only, before ANTE/BLINDS) ────────────────────
    # The name is matched non-greedily up to the stack size, because pseudonyms
    # contain spaces. Requiring \S+ dropped those players from the table, and
    # since positions are derived from the *count* of occupied seats, one
    # missing opponent shifted every label in the hand.
    header_section = block.split("*** ANTE/BLINDS ***")[0]
    seats: dict[int, dict] = {}
    for m in re.finditer(r"^Seat (\d+): (.+?) \((\d+)", header_section, re.M):
        seats[int(m.group(1))] = {"player": m.group(2), "chips": int(m.group(3))}

    if not hero or not any(v["player"] == hero for v in seats.values()):
        return None

    hero_seat = next(k for k, v in seats.items() if v["player"] == hero)
    starting_chips = seats[hero_seat]["chips"]
    hero_position = _calc_position(hero_seat, btn_seat, sorted(seats.keys()))

    # ── Hero cards ──────────────────────────────────────────────────────────
    cm = re.search(rf"Dealt to {re.escape(hero)} \[(.+?)\]", block)
    hero_cards = cm.group(1).replace(" ", "") if cm else None

    # ── Action log ──────────────────────────────────────────────────────────
    actions = _street_actions(block, {v["player"] for v in seats.values()})
    preflop = _preflop_stats(actions["preflop"], hero)
    cbet_flop_opp, cbet_flop = _cbet_flop(actions["flop"], hero, preflop["last_aggressor"])

    # ── Streets ─────────────────────────────────────────────────────────────
    # "The hero saw the flop", not "a flop was dealt": the two differ every time
    # the hero folds pre-flop and the other players carry on, which is most
    # hands. Same for the showdown — it happens without the hero all the time.
    hero_folded = preflop["folded_preflop"] or any(
        player == hero and verb == "folds"
        for street in _POSTFLOP_STREETS
        for player, verb, _ in actions[street]
    )
    saw_flop = "*** FLOP ***" in block and not preflop["folded_preflop"]
    went_to_showdown = "*** SHOW DOWN ***" in block and not hero_folded

    # ── Chip flows ──────────────────────────────────────────────────────────
    # An all-in against shorter stacks splits the pot: Winamax then writes
    # "from main pot" and "from side pot 1" instead of plain "from pot".
    # Matching only the plain form silently dropped those winnings, and the
    # hero's net came out short by the whole side pot every time.
    chips_won = sum(
        int(m.group(1))
        for m in re.finditer(
            rf"{re.escape(hero)} collected (\d+) from (?:main |side )?pot", block
        )
    )
    won_hand = chips_won > 0

    invested = _chips_invested(block, hero)
    net_chips = chips_won - invested

    return {
        "hand_id": hand_id,
        "tournament_id": tid,
        "table_name": table_name,
        "level": level,
        "ante": ante,
        "small_blind": small_blind,
        "big_blind": big_blind,
        "hand_datetime": hand_datetime,
        "hero": hero,
        "hero_seat": hero_seat,
        "hero_position": hero_position,
        "hero_cards": hero_cards,
        "starting_chips": starting_chips,
        "saw_flop": saw_flop,
        "went_to_showdown": went_to_showdown,
        "won_hand": won_hand,
        "chips_won": chips_won,
        "chips_invested": invested,
        "net_chips": net_chips,
        "vpip": preflop["vpip"],
        "pfr": preflop["pfr"],
        "pf_3bet_opp": preflop["pf_3bet_opp"],
        "pf_3bet": preflop["pf_3bet"],
        "faced_3bet": preflop["faced_3bet"],
        "folded_to_3bet": preflop["folded_to_3bet"],
        "cbet_flop_opp": cbet_flop_opp,
        "cbet_flop": cbet_flop,
        **_postflop_counts(actions, hero),
    }


# ── Action log ────────────────────────────────────────────────────────────────

# The betting streets, in order. ANTE/BLINDS is deliberately absent: blinds and
# antes are forced, and counting them as actions would turn every big blind into
# a call. SHOW DOWN and SUMMARY carry no betting either — SUMMARY in particular
# recaps lines that were already seen and would double every count.
_POSTFLOP_STREETS = ("flop", "turn", "river")
_STREETS = ("preflop",) + _POSTFLOP_STREETS

_STREET_BY_MARKER = {
    "*** PRE-FLOP ***": "preflop",
    "*** FLOP ***": "flop",
    "*** TURN ***": "turn",
    "*** RIVER ***": "river",
}

# "<player> <verb>[ <amount>][ to <total>][ and is all-in]"
# The player group is non-greedy and the verb list closed, so a pseudonym made
# of several words is captured whole; the match is then checked against the
# players actually seated, which rules out any line that merely reads like one.
_ACTION_RE = re.compile(
    r"^(?P<player>.+?) (?P<verb>folds|checks|calls|bets|raises)\b"
    r"(?: (?P<amount>\d+))?(?: to (?P<total>\d+))?"
)


def _street_actions(block: str, players: set[str]) -> dict[str, list[tuple[str, str, int | None]]]:
    """
    Split one hand into (player, verb, amount) triples, keyed by street.

    `amount` follows the same convention as _chips_invested: the bet or call
    size, and for a raise the "to" total rather than the increment Winamax
    prints first. It is None for checks and folds.
    """
    actions: dict[str, list[tuple[str, str, int | None]]] = {s: [] for s in _STREETS}
    street: str | None = None

    for line in block.splitlines():
        if line.startswith("*** "):
            street = next(
                (s for marker, s in _STREET_BY_MARKER.items() if line.startswith(marker)),
                None,
            )
            continue
        if street is None:
            continue
        m = _ACTION_RE.match(line)
        if m is None or m.group("player") not in players:
            continue
        amount = m.group("total") or m.group("amount")
        actions[street].append(
            (m.group("player"), m.group("verb"), int(amount) if amount else None)
        )

    return actions


def _preflop_stats(actions: list[tuple[str, str, int | None]], hero: str) -> dict:
    """
    Everything the pre-flop street says about the hero, in one pass.

    Blinds are posted above the PRE-FLOP marker, so the first "raises" of the
    street is the open, the second is the 3-bet and the third the 4-bet — the
    running `raises` count is all that is needed to name a spot.

    Each spot is recorded at most once, the first time the hero faces it, and
    the opportunity is stored next to the action: a 3-bet percentage is
    3-bets over *spots faced*, and averaging per-hand percentages afterwards
    would give the wrong answer. Same reasoning for every other rate below.

    `last_aggressor` is whoever put in the final raise — the player entitled to
    a continuation bet on the flop. It is None in a limped pot, where nobody is
    continuing anything.
    """
    out = {
        "vpip": False,
        "pfr": False,
        "pf_3bet_opp": False,
        "pf_3bet": False,
        "faced_3bet": False,
        "folded_to_3bet": False,
        "folded_preflop": False,
        "last_aggressor": None,
    }

    raises = 0
    hero_opened = False     # the hero made raise #1, so a re-raise is aimed at them

    for player, verb, _ in actions:
        if player == hero:
            if verb == "raises":
                out["pfr"] = out["vpip"] = True
            elif verb == "calls":
                out["vpip"] = True
            elif verb == "folds":
                out["folded_preflop"] = True

            # Facing a lone raise the hero didn't make: re-raising here is the
            # 3-bet. A limp in front changes nothing — a limp is not a raise.
            if raises == 1 and not hero_opened and not out["pf_3bet_opp"]:
                out["pf_3bet_opp"] = True
                out["pf_3bet"] = verb == "raises"

            # The hero opened and someone came over the top. Restricted to
            # hero_opened so that folding to a 4-bet after 3-betting — a
            # different spot entirely — is not counted here.
            if hero_opened and raises >= 2 and not out["faced_3bet"]:
                out["faced_3bet"] = True
                out["folded_to_3bet"] = verb == "folds"

        if verb == "raises":
            raises += 1
            out["last_aggressor"] = player
            if player == hero and raises == 1:
                hero_opened = True

    return out


def _cbet_flop(
    flop_actions: list[tuple[str, str, int | None]],
    hero: str,
    last_aggressor: str | None,
) -> tuple[bool, bool]:
    """
    (opportunity, bet) for a flop continuation bet.

    The hero must have taken the betting lead pre-flop and still be first to put
    chips in on the flop. A donk bet ahead of the hero cancels the opportunity:
    there is no lead left to continue, and a raise there is a different stat.
    Checking behind counts as a missed c-bet, which is the point of tracking it.
    """
    if last_aggressor != hero:
        return False, False

    for player, verb, _ in flop_actions:
        if player == hero:
            return True, verb == "bets"
        if verb == "bets":
            return False, False

    # Flop dealt but the hero never acted — all-in pre-flop, typically.
    return False, False


def _postflop_counts(actions: dict[str, list[tuple[str, str, int | None]]], hero: str) -> dict:
    """
    Raw action counts feeding the aggression factor, post-flop only.

    Aggression factor is (bets + raises) / calls and aggression frequency adds
    folds to the denominator; both are conventionally post-flop, since pre-flop
    is dominated by the forced blinds. Checks stay out of every formula.
    Storing the four counts rather than a per-hand ratio is what lets them be
    summed into a correct session-wide figure.
    """
    counts = {
        "postflop_bets": 0,
        "postflop_raises": 0,
        "postflop_calls": 0,
        "postflop_folds": 0,
    }
    column = {
        "bets": "postflop_bets",
        "raises": "postflop_raises",
        "calls": "postflop_calls",
        "folds": "postflop_folds",
    }

    for street in _POSTFLOP_STREETS:
        for player, verb, _ in actions[street]:
            if player == hero and verb in column:
                counts[column[verb]] += 1

    return counts


# ── Chip accounting ───────────────────────────────────────────────────────────

# A new betting round zeroes what everyone has committed. PRE-FLOP is absent on
# purpose: the blinds are posted in the ANTE/BLINDS section *above* that marker
# and stay live for the pre-flop round, so resetting there would double-count
# the blind on any raise the hero makes from the SB or BB.
_STREET_RESETS = ("*** FLOP ***", "*** TURN ***", "*** RIVER ***")

_HERO_ACTIONS = (
    # (pattern, is_raise) — matched against the text following "<hero> "
    (re.compile(r"posts ante (\d+)"), False),
    (re.compile(r"posts (?:small|big) blind (\d+)"), False),
    (re.compile(r"(?:bets|calls) (\d+)"), False),
    (re.compile(r"raises \d+ to (\d+)"), True),
)


def _chips_invested(block: str, hero: str) -> int:
    """
    Total chips leaving the hero's stack over one hand.

    Winamax writes "raises X to Y" where X is the increment above the *current
    bet level* and Y is the hero's new total *for that street*. The chips that
    actually leave the stack are therefore Y minus whatever the hero had already
    committed on that street — not X. The two only coincide when the hero's
    committed amount already equals the bet level (the BB raising an unopened
    pot); for an opening raiser they differ by a full blind every hand.

    `committed` tracks the hero's total for the current street and resets on
    each new betting round. Antes are added to the running total but stay out of
    `committed`: Winamax posts them outside the betting line, so they are not
    part of the "to Y" figure.

    Trailing " and is all-in" on any of these lines is matched over, since every
    pattern below anchors at the start of the action and stops at its amount.
    """
    invested = 0
    committed = 0          # hero's chips already in on the current street
    prefix = hero + " "

    for line in block.splitlines():
        if line.startswith("*** "):
            if line.startswith("*** SUMMARY ***"):
                break      # recap section — every action has already been seen
            if line.startswith(_STREET_RESETS):
                committed = 0
            continue

        if not line.startswith(prefix):
            continue
        action = line[len(prefix):]

        for pattern, is_raise in _HERO_ACTIONS:
            m = pattern.match(action)
            if not m:
                continue
            amount = int(m.group(1))
            if is_raise:
                invested += amount - committed   # Y - already committed
                committed = amount
            else:
                invested += amount
                if not action.startswith("posts ante"):
                    committed += amount          # antes sit outside the bet line
            break

    return invested


# ── Position helpers ──────────────────────────────────────────────────────────

def _calc_position(hero_seat: int, btn_seat: int | None, seats_sorted: list[int]) -> str | None:
    """
    Returns a position label (BTN, CO, HJ, UTG…, SB, BB) based on active seats.
    Works with non-consecutive seat numbers (empty seats skipped).

    The button may sit on a seat nobody occupies — it stays there for one orbit
    after that player busts, which happens constantly late in a tournament. The
    dead seat still takes its turn in the rotation, so it joins the ring and is
    then skipped when the labels are handed out: the first player after a dead
    button is the small blind, not the button.
    """
    if not hero_seat or not btn_seat or not seats_sorted:
        return None

    occupied = set(seats_sorted)
    ring = sorted(occupied | {btn_seat})
    n = len(ring)
    if n < 2 or hero_seat not in occupied:
        return None

    btn_idx = ring.index(btn_seat)
    rotated = ring[btn_idx:] + ring[:btn_idx]
    clockwise = [s for s in rotated if s in occupied]

    # Steps clockwise from button, the dead seat counting as step 0
    steps = clockwise.index(hero_seat) + (0 if btn_seat in occupied else 1)

    if n == 2:
        return "BTN/SB" if steps == 0 else "BB"

    pos_map = {0: "BTN", 1: "SB", 2: "BB"}
    # Fill early/middle positions from the BTN working backwards
    early = ["UTG", "UTG+1", "UTG+2", "MP", "HJ", "CO"]
    remaining = n - 3           # seats that are neither BTN/SB/BB
    for i in range(remaining):
        # steps 3 … n-1  →  UTG … CO  (CO is always step n-1)
        label_idx = len(early) - remaining + i
        pos_map[3 + i] = early[max(0, label_idx)]

    return pos_map.get(steps, f"P{steps}")
