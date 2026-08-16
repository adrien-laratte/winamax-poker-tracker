"""
Bankroll ledger — the .ods the player keeps in the export folder.

One row per session in the "Suivi" sheet, written as each session closes, plus
the cached figures of the "Dashboard" sheet.

The file belongs to the player, so it is never rebuilt from the database: rows
are added, or corrected in place when a session is exported a second time, and
everything else — deposits, notes, styles, column widths, data validations —
is left exactly as it was found. A session is recognised by its identifier,
written in the first free column, which is what allows a correction to land on
the right row instead of appending a duplicate.

Written with the standard library rather than an ODF toolkit. An .ods is a zip
of XML documents and the job here is narrow: fill cells that already exist and
refresh the values cached next to the formulas. That keeps the container, the
styles and every sheet not mentioned here byte-for-byte identical, and adds no
dependency to install on the NAS.

Values are written next to the formulas on purpose. LibreOffice does not
recalculate an ODF file on load by default — it trusts the value cached in the
document — so a row carrying only a formula would show up blank until the sheet
happened to be recalculated.
"""

import copy
import io
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime

CONTENT = "content.xml"

NS = {
    "office":  "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "table":   "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "text":    "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "calcext": "urn:org:documentfoundation:names:experimental:calc:xmlns:calcext:1.0",
}

SHEET_LEDGER    = "Suivi"
SHEET_DASHBOARD = "Dashboard"

# Layout of the "Suivi" sheet, 1-based like the spreadsheet itself.
START_ROW  = 4      # B4 holds the opening bankroll
HEADER_ROW = 5
FIRST_ROW  = 6      # first row the Dashboard sums over
LAST_ROW   = 305    # last one — the ranges in the Dashboard stop there
MODEL_ROW  = 7      # first session written by hand: the row new ones copy from

(DATE, SITE, TOURNAMENTS, DURATION, INVESTED, WON,
 PROFIT, ROI, BANKROLL, PER_HOUR, NOTES, REF) = range(1, 13)

REF_HEADER = "Réf. session"

# The sheet's own formulas, kept identical to what the player wrote, with one
# correction: a duration is stored by LibreOffice as a fraction of a day, so
# dividing a profit by it gave a figure 24 times too small. See _fix_per_hour.
F_PROFIT   = 'of:=IF([.A{r}]="";"";[.F{r}]-[.E{r}])'
F_ROI      = 'of:=IF([.A{r}]="";"";IF([.E{r}]=0;"";[.G{r}]/[.E{r}]))'
F_BANKROLL = 'of:=IF([.A{r}]="";"";IF([.I{p}]="";[.$B$4];[.I{p}])+[.G{r}])'
F_PER_HOUR = 'of:=IF([.A{r}]="";"";IF([.D{r}]=0;"";[.G{r}]/([.D{r}]*24)))'

# Dashboard: (row, formula or None to leave it alone, key of the computed value)
F_HOURS = "of:=SUM([$Suivi.D6:.D305])*24"


class LedgerError(Exception):
    """Anything that stops this module from filling the spreadsheet."""


class LedgerLocked(LedgerError):
    """The spreadsheet is open in LibreOffice; writing now would be lost."""


class LedgerShape(LedgerError):
    """The file is not the ledger this module knows how to fill."""


# ── XML helpers ───────────────────────────────────────────────────────────────

def _q(prefix: str, name: str) -> str:
    return f"{{{NS[prefix]}}}{name}"


CELL       = _q("table", "table-cell")
COVERED    = _q("table", "covered-table-cell")
ROW        = _q("table", "table-row")
REPEAT_COL = _q("table", "number-columns-repeated")
REPEAT_ROW = _q("table", "number-rows-repeated")
STYLE      = _q("table", "style-name")
FORMULA    = _q("table", "formula")
VALUE_TYPE = _q("office", "value-type")
CALC_TYPE  = _q("calcext", "value-type")
P          = _q("text", "p")

# Everything that carries a cell's content, cleared before a new value is set.
# The style, the validation and any other attribute are the file's own.
_VALUE_ATTRS = (
    VALUE_TYPE, CALC_TYPE, FORMULA,
    _q("office", "value"), _q("office", "date-value"), _q("office", "time-value"),
    _q("office", "string-value"), _q("office", "boolean-value"),
)


def _repeats(el, attr: str) -> int:
    try:
        return max(1, int(el.get(attr, 1)))
    except (TypeError, ValueError):
        return 1


def _walk(parent, tags: tuple, attr: str):
    """(index, element, repeat) for children of `tags`, 1-based, repeats intact."""
    pos = 1
    for el in list(parent):
        if el.tag in tags:
            rep = _repeats(el, attr)
            yield pos, el, rep
            pos += rep


def _split(parent, el, rep: int, offset: int, attr: str):
    """
    Give the element at `offset` inside a repeated run an element of its own.

    A run of identical empty cells is stored once with a repeat count; writing
    into one of them means cutting the run into up to three pieces so the
    middle one can carry a value without dragging its neighbours along.
    """
    if rep == 1:
        return el
    def run(size: int):
        """The untouched part of the run, as one element carrying its count."""
        piece = copy.deepcopy(el)
        if size > 1:
            piece.set(attr, str(size))
        else:
            piece.attrib.pop(attr, None)
        return piece

    at = list(parent).index(el)
    target = copy.deepcopy(el)
    target.attrib.pop(attr, None)

    before, after = offset, rep - offset - 1
    pieces = ([run(before)] if before else []) + [target] + ([run(after)] if after else [])

    parent.remove(el)
    for i, piece in enumerate(pieces):
        parent.insert(at + i, piece)
    return target


def _row(sheet, index: int, create: bool = False):
    """The row element at `index` (1-based), or None."""
    for pos, el, rep in _walk(sheet, (ROW,), REPEAT_ROW):
        if pos <= index < pos + rep:
            return _split(sheet, el, rep, index - pos, REPEAT_ROW) if create else el
    if create:
        raise LedgerShape(f"la feuille s'arrête avant la ligne {index}")
    return None


def _cell(row, col: int, create: bool = False):
    """The cell element at `col` (1-based), or None."""
    if row is None:
        return None
    last = None
    for pos, el, rep in _walk(row, (CELL, COVERED), REPEAT_COL):
        if pos <= col < pos + rep:
            return _split(row, el, rep, col - pos, REPEAT_COL) if create else el
        last = pos + rep
    if not create:
        return None
    # Past the end of the row: pad with blanks, then the cell asked for.
    if last is not None and col > last:
        gap = col - last
        if gap:
            filler = ET.SubElement(row, CELL)
            if gap > 1:
                filler.set(REPEAT_COL, str(gap))
    return ET.SubElement(row, CELL)


def _text(cell) -> str:
    if cell is None:
        return ""
    return "".join("".join(p.itertext()) for p in cell.findall(P)).strip()


def _number(cell) -> float | None:
    if cell is None:
        return None
    raw = cell.get(_q("office", "value"))
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


def _date(cell) -> date | None:
    raw = cell.get(_q("office", "date-value")) if cell is not None else None
    try:
        return datetime.fromisoformat(raw).date() if raw else None
    except ValueError:
        return None


def _hours(cell) -> float:
    """A duration cell, in hours. ODF spells it PT03H48M00S."""
    raw = cell.get(_q("office", "time-value")) if cell is not None else None
    if not raw or not raw.startswith("PT"):
        return 0.0
    total, digits = 0.0, ""
    for ch in raw[2:]:
        if ch.isdigit() or ch == ".":
            digits += ch
        else:
            value = float(digits or 0)
            total += value * {"H": 1, "M": 1 / 60, "S": 1 / 3600}.get(ch, 0)
            digits = ""
    return total


def _set(cell, *, kind=None, value=None, text="", formula=None, style=None):
    """Replace a cell's content, leaving its style and validation untouched."""
    for attr in _VALUE_ATTRS:
        cell.attrib.pop(attr, None)
    for p in cell.findall(P):
        cell.remove(p)
    if style:
        cell.set(STYLE, style)
    if formula:
        cell.set(FORMULA, formula)
    if kind:
        cell.set(VALUE_TYPE, kind)
        cell.set(CALC_TYPE, kind)
        attr = {
            "float": "value", "percentage": "value", "currency": "value",
            "date": "date-value", "time": "time-value", "string": "string-value",
        }[kind]
        if value is not None:
            cell.set(_q("office", attr), value)
    if text or kind:
        ET.SubElement(cell, P).text = text


# ── Formatting, the way the sheet already shows things ────────────────────────

def _fr(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def _money(value: float) -> str:
    return f"{_fr(value)} €"


def _pct(value: float) -> str:
    return f"{_fr(value * 100, 1)}%"


def _iso_duration(seconds: float) -> tuple[str, str]:
    h, rest = divmod(int(round(seconds)), 3600)
    m, s = divmod(rest, 60)
    return f"PT{h:02d}H{m:02d}M{s:02d}S", f"{h:02d}:{m:02d}:{s:02d}"


# ── The ledger ────────────────────────────────────────────────────────────────

class Ledger:
    """
    The bankroll spreadsheet, opened for one update and closed again.

        led = Ledger(path).open()
        led.record(ref=..., day=..., tournaments=..., seconds=..., ...)
        led.save()
    """

    def __init__(self, path: str):
        self.path = path
        self._root = None
        self._ledger = None
        self._dashboard = None

    # ── open / save ──────────────────────────────────────────────────────────

    def open(self) -> "Ledger":
        if self.locked():
            raise LedgerLocked(self.path)
        with zipfile.ZipFile(self.path) as z:
            xml = z.read(CONTENT)
        self._namespaces = _declared_namespaces(xml)
        for prefix, uri in self._namespaces:
            ET.register_namespace(prefix, uri)
        try:
            self._root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise LedgerShape(f"{self.path} illisible : {exc}") from exc

        sheets = {t.get(_q("table", "name")): t
                  for t in self._root.iter(_q("table", "table"))}
        if SHEET_LEDGER not in sheets:
            raise LedgerShape(f"feuille « {SHEET_LEDGER} » absente de {self.path}")
        self._ledger = sheets[SHEET_LEDGER]
        self._dashboard = sheets.get(SHEET_DASHBOARD)
        return self

    def locked(self) -> bool:
        """LibreOffice drops a .~lock.<name># next to a file it has open."""
        folder, name = os.path.split(os.path.abspath(self.path))
        return os.path.exists(os.path.join(folder, f".~lock.{name}#"))

    def save(self) -> None:
        """
        Rewrite the package, content.xml apart, then swap it in one move.

        A spreadsheet half-written is a spreadsheet lost, and this one holds
        figures that exist nowhere else — the deposits and the notes are the
        player's alone.
        """
        if self.locked():
            raise LedgerLocked(self.path)
        payload = _restore_namespaces(
            ET.tostring(self._root, encoding="UTF-8", xml_declaration=True),
            self._namespaces,
        )
        tmp = f"{self.path}.tmp"
        with zipfile.ZipFile(self.path) as src, \
             zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
            # An ODF package must open on its uncompressed mimetype entry.
            if "mimetype" in src.namelist():
                info = src.getinfo("mimetype")
                info.compress_type = zipfile.ZIP_STORED
                out.writestr(info, src.read("mimetype"))
            for info in src.infolist():
                if info.filename == "mimetype":
                    continue
                out.writestr(info, payload if info.filename == CONTENT
                             else src.read(info.filename))
        os.replace(tmp, self.path)

    # ── reading ──────────────────────────────────────────────────────────────

    @property
    def opening_bankroll(self) -> float:
        return _number(_cell(_row(self._ledger, START_ROW), 2)) or 0.0

    def _model_style(self, col: int) -> str | None:
        cell = _cell(_row(self._ledger, MODEL_ROW), col)
        return cell.get(STYLE) if cell is not None else None

    def rows(self) -> list[dict]:
        """Every row of the ledger that holds something, in sheet order."""
        out = []
        for index in range(FIRST_ROW, LAST_ROW + 1):
            row = _row(self._ledger, index)
            if row is None:
                continue
            cells = {c: _cell(row, c) for c in (DATE, TOURNAMENTS, DURATION,
                                                INVESTED, WON, PROFIT, NOTES, REF)}
            entry = {
                "row": index,
                "date": _date(cells[DATE]),
                "ref": _text(cells[REF]),
                "tournaments": _number(cells[TOURNAMENTS]),
                "hours": _hours(cells[DURATION]),
                "invested": _number(cells[INVESTED]),
                "won": _number(cells[WON]),
                "notes": _text(cells[NOTES]),
            }
            entry["profit"] = (entry["won"] or 0.0) - (entry["invested"] or 0.0)
            entry["filled"] = any(entry[k] is not None for k in
                                  ("tournaments", "invested", "won")) or bool(entry["notes"])
            if entry["date"] or entry["ref"] or entry["filled"]:
                out.append(entry)
        return out

    def bankroll_before(self, ref: str) -> float:
        """
        What the bankroll was worth when that session started.

        The session's own row is left out, so re-exporting a session reports
        the same figure as the first time rather than counting it twice.
        """
        total = self.opening_bankroll
        for entry in self.rows():
            if entry["ref"] == ref:
                break
            if entry["filled"]:
                total += entry["profit"]
        return round(total, 2)

    # ── writing ──────────────────────────────────────────────────────────────

    def tidy(self) -> int:
        """
        Clear the dates left on rows nothing was ever written into.

        The file shipped with ten days pre-dated ahead of time. The Dashboard
        counts a session per non-empty date, and the bankroll column chains on
        the row above it — an empty dated row therefore both inflated the
        session count and broke the chain. Returns how many were cleared.
        """
        cleared = 0
        for entry in self.rows():
            if entry["date"] and not entry["filled"] and not entry["ref"]:
                _set(_cell(_row(self._ledger, entry["row"], create=True), DATE, create=True))
                cleared += 1
        return cleared

    def record(self, *, ref: str, day: date, tournaments: int, seconds: float,
               invested: float, won: float, site: str = "Winamax") -> int:
        """
        Write one session. Returns the row it landed on.

        A session already in the file is corrected in place — that is what
        makes a re-export, or a parser fix reaching back over old files, land
        on the row it belongs to instead of appending a second one.
        """
        entries = self.rows()
        known = next((e for e in entries if e["ref"] == ref), None)
        if known:
            index = known["row"]
        else:
            used = [e["row"] for e in entries if e["filled"] or e["date"] or e["ref"]]
            index = max(used) + 1 if used else MODEL_ROW
            if index > LAST_ROW:
                raise LedgerShape(
                    f"le tableau est plein (ligne {LAST_ROW} atteinte) — "
                    "ajoute des lignes avant la prochaine session"
                )

        row = _row(self._ledger, index, create=True)
        profit = round(won - invested, 2)
        hours = seconds / 3600
        iso, shown = _iso_duration(seconds)

        def put(col, **kw):
            _set(_cell(row, col, create=True), **kw)

        put(DATE, kind="date", value=day.isoformat(), text=f"{day:%d/%m/%Y}")
        put(SITE, kind="string", text=site)
        put(TOURNAMENTS, kind="float", value=str(tournaments), text=str(tournaments))
        # The one column whose style has to be forced: on every row but the
        # first, D shares the plain-number style of C, and a duration written
        # there would read as "0,16" instead of "03:48:00".
        put(DURATION, kind="time", value=iso, text=shown, style=self._model_style(DURATION))
        put(INVESTED, kind="float", value=f"{invested:.2f}", text=_money(invested))
        put(WON, kind="float", value=f"{won:.2f}", text=_money(won))
        put(REF, kind="string", text=ref)

        # The computed columns, formula and value together. Rows 8 to 17 of the
        # shipped file had lost their profit formula along the way; writing it
        # back is what makes them add up again.
        put(PROFIT, kind="float", value=f"{profit:.2f}", text=_money(profit),
            formula=F_PROFIT.format(r=index))
        if invested:
            put(ROI, kind="percentage", value=f"{profit / invested:.6f}",
                text=_pct(profit / invested), formula=F_ROI.format(r=index))
        else:
            put(ROI, formula=F_ROI.format(r=index))
        if hours:
            put(PER_HOUR, kind="float", value=f"{profit / hours:.2f}",
                text=_money(profit / hours), formula=F_PER_HOUR.format(r=index))
        else:
            put(PER_HOUR, formula=F_PER_HOUR.format(r=index))

        self._header()
        self._refresh_chain()
        self._fix_per_hour()
        self._dashboard_values()
        return index

    def _refresh_chain(self) -> None:
        """
        Recompute the running bankroll down the column, value and formula.

        Correcting one session moves every bankroll figure under it. The
        formulas chain on their own once the sheet is recalculated, but the
        values cached beside them would still show what the file said before —
        and this one is only recalculated when something is typed into it.
        """
        running = self.opening_bankroll
        for entry in self.rows():
            if not entry["filled"]:
                continue
            running = round(running + entry["profit"], 2)
            cell = _cell(_row(self._ledger, entry["row"], create=True), BANKROLL, create=True)
            _set(cell, kind="float", value=f"{running:.2f}", text=_money(running),
                 formula=F_BANKROLL.format(r=entry["row"], p=entry["row"] - 1),
                 style=cell.get(STYLE))

    def _header(self) -> None:
        """Name the identifier column, so the file explains itself."""
        header = _row(self._ledger, HEADER_ROW, create=True)
        cell = _cell(header, REF, create=True)
        if _text(cell) != REF_HEADER:
            neighbour = _cell(header, NOTES)
            _set(cell, kind="string", text=REF_HEADER,
                 style=neighbour.get(STYLE) if neighbour is not None else cell.get(STYLE))

    def _fix_per_hour(self) -> None:
        """
        Correct the €/hour formula on rows written before this module existed.

        LibreOffice stores 03:48:00 as 0.158 — a fraction of a day — so the
        original G/D divided a profit by a sixth of a day and reported a figure
        24 times too small. The cached value is refreshed along with it.
        """
        for entry in self.rows():
            if not entry["filled"]:
                continue
            row = _row(self._ledger, entry["row"], create=True)
            cell = _cell(row, PER_HOUR, create=True)
            wanted = F_PER_HOUR.format(r=entry["row"])
            if cell.get(FORMULA) == wanted:
                continue
            if entry["hours"]:
                rate = entry["profit"] / entry["hours"]
                _set(cell, kind="float", value=f"{rate:.2f}", text=_money(rate),
                     formula=wanted, style=cell.get(STYLE))
            else:
                _set(cell, formula=wanted, style=cell.get(STYLE))

    def _dashboard_values(self) -> None:
        """
        Refresh the figures the Dashboard caches, and correct its hours.

        Same fraction-of-a-day trap: "Heures jouées" summed the durations raw,
        so it read 0,2 for a session of nearly four hours, and every average
        per hour built on it was wrong by the same factor.
        """
        if self._dashboard is None:
            return
        played = [e for e in self.rows() if e["filled"]]
        profits = [e["profit"] for e in played]
        opening = self.opening_bankroll
        hours = sum(e["hours"] for e in played)
        invested = sum(e["invested"] or 0.0 for e in played)
        won = sum(e["won"] or 0.0 for e in played)
        net = sum(profits)
        # Mirrors COUNTIF(A6:A305;"<>"): a dated row is a session.
        count = len([e for e in self.rows() if e["date"]])

        figures = {
            4:  (opening, _money),
            5:  (count, lambda v: str(int(v))),
            6:  (sum(e["tournaments"] or 0 for e in played), lambda v: str(int(v))),
            7:  (hours, lambda v: _fr(v, 1)),
            8:  (invested, _money),
            9:  (won, _money),
            10: (net, _money),
            11: (opening + net, _money),
            12: (net / invested if invested else None, _pct),
            13: (net / count if count else None, _money),
            14: (net / hours if hours else None, _money),
            15: (max(profits) if profits else None, _money),
            16: (min(profits) if profits else None, _money),
        }
        for index, (value, show) in figures.items():
            cell = _cell(_row(self._dashboard, index, create=True), 2, create=True)
            formula = F_HOURS if index == 7 else cell.get(FORMULA)
            if value is None:
                _set(cell, formula=formula, style=cell.get(STYLE))
                continue
            kind = "percentage" if index == 12 else "float"
            _set(cell, kind=kind, value=f"{value:.6f}" if kind == "percentage"
                 else f"{value:.2f}", text=show(value), formula=formula,
                 style=cell.get(STYLE))


def _declared_namespaces(xml: bytes) -> list[tuple[str, str]]:
    """Keep the file's own prefixes, so the XML stays the one the player knows."""
    return [pair for _, pair in ET.iterparse(io.BytesIO(xml), events=("start-ns",))]


def _restore_namespaces(payload: bytes, declared: list[tuple[str, str]]) -> bytes:
    """
    Put back the namespace declarations ElementTree leaves out.

    It only declares what it finds in use in the tree — and a formula is opaque
    text as far as it is concerned. Nothing carries the "of" prefix that every
    "of:=IF(...)" opens with, so its declaration would be dropped and every
    formula in the file left pointing at a namespace that no longer exists.
    """
    root = payload.index(b"<", payload.index(b"?>") + 2)
    end = payload.index(b">", root)
    present = set(re.findall(rb"xmlns:([\w.-]+)=", payload[root:end]))
    missing = b"".join(
        f' xmlns:{prefix}="{uri}"'.encode()
        for prefix, uri in declared
        if prefix and prefix.encode() not in present
    )
    return payload[:end] + missing + payload[end:]


# ── The one call the exporter makes ───────────────────────────────────────────

def record_session(path: str, *, session_id: str, day: date, tournaments: int,
                   seconds: float, invested: float, won: float) -> int:
    """
    Add or correct this session's row; returns the row it landed on.

    Raises LedgerLocked when the spreadsheet is open in LibreOffice, which the
    caller is expected to treat as "later": nothing is lost, the session is
    written on the next pass.
    """
    led = Ledger(path).open()
    led.tidy()
    index = led.record(ref=session_id, day=day, tournaments=tournaments,
                       seconds=seconds, invested=invested, won=won)
    led.save()
    return index


def opening_for(path: str, session_id: str) -> float | None:
    """
    The bankroll a session started from, for the workbook's bankroll block.

    None whenever the file cannot be read — a locked or missing ledger leaves
    the cell to be typed into, exactly as before, rather than failing an export.
    """
    try:
        return Ledger(path).open().bankroll_before(session_id)
    except (LedgerError, OSError):
        return None


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "bankroll_poker_mtt.ods"
    book = Ledger(target).open()
    print(f"{target}  —  bankroll de départ {_money(book.opening_bankroll)}")
    for item in book.rows():
        day = f"{item['date']:%d/%m/%Y}" if item["date"] else "—".ljust(10)
        print(f"  L{item['row']:>3}  {day}  {item['tournaments'] or '':>3} tournois  "
              f"{item['hours']:>5.2f} h  profit {item['profit']:>8.2f}  {item['ref']}")
