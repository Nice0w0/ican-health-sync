"""
Core conversion: CGM .xls upload -> list of readings.

Standard library only, deliberately. That is what lets this deploy to a free
serverless tier with no build step and no dependency to keep patched, and it is
why the whole thing can be run by anyone who wants it without asking the author
to host anything.

Nothing here writes to disk or keeps state between calls.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

from xlsmini import XlsError, read_grid

# The export carries no timezone; it is written in the wearer's local time, so
# a server must not assume its own -- serverless regions are usually UTC.
TZ_OFFSET = float(os.environ.get("CGM_TZ_OFFSET", "7"))
TZ = timezone(timedelta(hours=TZ_OFFSET))
TOKEN = os.environ.get("CGM_TOKEN", "")

HEADER_COL0 = "เลขที่"
TIME_FORMAT = "%H:%M,%m/%d/%Y"  # "15:22,09/01/2026" -> 1 Sep 2026, 15:22
MAX_BYTES = 10 * 1024 * 1024



class BadRequest(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# Shortcuts' "Log Health Sample" takes its unit from a fixed picker -- it
# cannot be driven by a variable. So the unit is settled here instead: read
# whatever the export declares, convert to the one the Shortcut is set to, and
# the two can never disagree. This is not cosmetic -- 7.2 mmol/L logged as
# 7.2 mg/dL reads as severe hypoglycaemia.
MG_PER_MMOL = 18.0182
DEFAULT_UNIT = "mg/dL"
UNITS = {                      # keys are lowercased with spaces and dots removed
    "mg/dl": "mg/dL",
    "mgdl": "mg/dL",
    "มก/ดล": "mg/dL",           # Thai-localised exports
    "มก/ดล.": "mg/dL",
    "mmol/l": "mmol/L",
    "mmoll": "mmol/L",
    "มิลลิโมล/ลิตร": "mmol/L",
    "มิลลิโมล/ล": "mmol/L",
}


def canonical_unit(raw: str) -> str:
    unit = UNITS.get(raw.strip().lower().replace(" ", "").replace(".", ""))
    if unit is None:
        # Never guess a glucose unit; refusing is the safe failure.
        raise BadRequest(422, "unrecognised glucose unit %r -- expected mg/dL "
                              "or mmol/L" % raw)
    return unit


def convert_value(value: float, src: str, dst: str) -> float:
    if src == dst:
        converted = value
    elif src == "mmol/L" and dst == "mg/dL":
        converted = value * MG_PER_MMOL
    else:
        converted = value / MG_PER_MMOL
    # mg/dL is whole numbers in practice; mmol/L is conventionally 1 decimal.
    return round(converted) if dst == "mg/dL" else round(converted, 1)


def extract_upload(body: bytes, content_type: str) -> bytes:
    """
    Return the .xls bytes from either a raw body or a multipart form.

    Shortcuts sends one or the other depending on how "Get Contents of URL" is
    configured, and picks its own field name, so both shapes are accepted --
    a misconfigured shortcut should not turn into a confusing error.
    """
    if "multipart/form-data" not in content_type:
        return body

    marker = "boundary="
    if marker not in content_type:
        raise BadRequest(400, "multipart request without a boundary")
    boundary = content_type.split(marker, 1)[1].strip().strip('"')
    sep = b"--" + boundary.encode()

    for part in body.split(sep):
        head, _, payload = part.partition(b"\r\n\r\n")
        if not payload or b"filename=" not in head:
            continue
        return payload.rsplit(b"\r\n", 1)[0]
    raise BadRequest(400, "multipart request carried no file")


def read_rows(blob: bytes) -> tuple[list[tuple[datetime, float]], str]:
    """
    Return (rows oldest-first, unit declared by the export).

    Deliberately does no formatting and no unit conversion: most of a share is
    usually readings Health already has, and there is no point shaping rows
    that are about to be filtered away.
    """
    with NamedTemporaryFile(suffix=".xls") as fh:
        fh.write(blob)
        fh.flush()
        try:
            grid = read_grid(Path(fh.name))
        except XlsError as exc:
            # A file that is not a readable .xls is the caller's mistake, not a
            # server fault -- say so rather than returning 500.
            raise BadRequest(422, str(exc))

    header_row = None
    for i, row in enumerate(grid):
        if row and str(row[0]).strip() == HEADER_COL0:
            header_row = i
            break
    if header_row is None:
        raise BadRequest(422, "no '%s' header row -- not a CGM export" % HEADER_COL0)

    # The value column header declares the unit, e.g. "ค่ากลูโคส (mg/dL)".
    head = str(grid[header_row][2]) if len(grid[header_row]) > 2 else ""
    if "(" not in head or ")" not in head:
        raise BadRequest(422, "value column header %r does not declare a unit" % head)
    unit = canonical_unit(head[head.index("(") + 1:head.rindex(")")])

    rows = []
    for i in range(header_row + 1, len(grid)):
        row = grid[i]
        if len(row) < 3:
            continue
        raw_time, raw_value = str(row[1]).strip(), str(row[2]).strip()
        if not raw_time or not raw_value:
            continue
        try:
            # Hardcoded format on purpose: "09/01/2026" is ambiguous and no
            # locale guessing belongs anywhere near a medical timestamp.
            when = datetime.strptime(raw_time, TIME_FORMAT).replace(tzinfo=TZ)
        except ValueError:
            raise BadRequest(422, "row %d: unexpected time %r" % (i + 1, raw_time))
        try:
            value = float(raw_value)
        except ValueError:
            raise BadRequest(422, "row %d: value %r is not a number" % (i + 1, raw_value))
        rows.append((when, value))
    if not rows:
        raise BadRequest(422, "header found but no readings under it")
    rows.sort(key=lambda r: r[0])

    # Collapse repeated timestamps. HealthKit has no upsert -- Log Health
    # Sample only ever appends -- so a row that appears twice in the export
    # becomes two samples in Health at the same minute, and no later run can
    # remove them. Dropping them here is the only place it can be done.
    deduped = []
    for row in rows:
        if deduped and deduped[-1][0] == row[0]:
            deduped[-1] = row      # same minute: the later row wins
        else:
            deduped.append(row)
    return deduped, unit


def thin(rows: list[tuple[datetime, float]], minutes: int) -> list[tuple[datetime, float]]:
    """
    Drop readings closer together than `minutes`, newest kept first.

    A CGM samples every three minutes, so a day is ~480 readings and the
    Shortcut's `Repeat with Each` is what makes a big share slow -- four
    actions per reading, on-device. Thinning is the only lever that shortens
    that loop without losing the shape of the curve.

    Walking newest-to-oldest matters: it guarantees the most recent reading is
    always kept. Doing it oldest-first would sometimes drop the newest one,
    which reads as "it didn't sync the latest".
    """
    gap = timedelta(minutes=minutes)
    kept, last = [], None
    for when, value in reversed(rows):
        if last is None or last - when >= gap:
            kept.append((when, value))
            last = when
    kept.reverse()
    return kept


# Shortcuts renders a Date into a text field using the phone's own locale, so
# what arrives here depends on where the wearer lives. A Thai phone sends
# "3 ก.ย. 2569 23:22" -- Buddhist era, Thai month name -- and nothing about that
# is ISO 8601. Rejecting it would be correct and useless.
THAI_MONTHS = {
    "ม.ค.": 1, "มกราคม": 1, "ก.พ.": 2, "กุมภาพันธ์": 2,
    "มี.ค.": 3, "มีนาคม": 3, "เม.ย.": 4, "เมษายน": 4,
    "พ.ค.": 5, "พฤษภาคม": 5, "มิ.ย.": 6, "มิถุนายน": 6,
    "ก.ค.": 7, "กรกฎาคม": 7, "ส.ค.": 8, "สิงหาคม": 8,
    "ก.ย.": 9, "กันยายน": 9, "ต.ค.": 10, "ตุลาคม": 10,
    "พ.ย.": 11, "พฤศจิกายน": 11, "ธ.ค.": 12, "ธันวาคม": 12,
}
EN_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
BUDDHIST_OFFSET = 543


def parse_named_date(raw: str) -> datetime | None:
    """
    Read a date whose month is spelled out, in Thai or English.

    Only named months are accepted. "03/09/2026" is deliberately refused:
    the day/month order cannot be recovered from the string, and guessing
    wrong moves a glucose reading six months.
    """
    text = raw.strip()

    month = None
    for name, number in THAI_MONTHS.items():
        if name in text:
            month = number
            text = text.replace(name, " ")
            break
    if month is None:
        lowered = text.lower()
        for name, number in EN_MONTHS.items():
            if name in lowered:
                month = number
                index = lowered.index(name)
                # Cut the whole word, not just the three letters, so that
                # "September" does not leave "tember" behind.
                end = index
                while end < len(text) and text[end].isalpha():
                    end += 1
                text = text[:index] + " " + text[end:]
                break
    if month is None:
        return None

    clock = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    hour = minute = second = 0
    if clock:
        hour, minute = int(clock.group(1)), int(clock.group(2))
        second = int(clock.group(3) or 0)
        text = text[:clock.start()] + " " + text[clock.end():]
        meridiem = re.search(r"\b([AaPp])\.?[Mm]\.?", text)
        if meridiem:
            upper = meridiem.group(1).upper()
            if upper == "P" and hour < 12:
                hour += 12
            elif upper == "A" and hour == 12:
                hour = 0

    numbers = [int(n) for n in re.findall(r"\d+", text)]
    year = next((n for n in numbers if n >= 1000), None)
    day = next((n for n in numbers if 1 <= n <= 31), None)
    if year is None or day is None:
        return None
    if year > 2400:                       # Buddhist era
        year -= BUDDHIST_OFFSET

    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=TZ)
    except ValueError:
        return None


def parse_since(raw: str) -> datetime:
    """Accept what a Shortcut is likely to send, not only strict ISO."""
    text = raw.strip().replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        try:
            when = datetime.fromtimestamp(float(text), TZ)
        except ValueError:
            when = parse_named_date(raw)
            if when is None:
                raise BadRequest(400, "cannot read since=%r; use ISO 8601, a "
                                      "unix timestamp, or a date with a named "
                                      "month" % raw)
    # A naive timestamp means the caller's own clock, which is the wearer's.
    return when.replace(tzinfo=TZ) if when.tzinfo is None else when


def authorise(supplied: str) -> None:
    if not TOKEN:
        return  # open by choice; documented
    # Constant-time-ish compare so the token cannot be probed byte by byte.
    if len(supplied) != len(TOKEN) or not all(a == b for a, b in zip(supplied, TOKEN)):
        raise BadRequest(401, "bad token")


def positive_int(query: dict, key: str) -> int | None:
    raw = query.get(key)
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise BadRequest(400, "%s must be a whole number, got %r" % (key, raw))
    if value < 1:
        raise BadRequest(400, "%s must be at least 1" % key)
    return value


def convert(body: bytes, content_type: str, query: dict):
    """
    Return (readings, total_before_filtering, output_unit, source_unit, cutoff).

    The order here is the whole point: filter, thin, cap, and only then shape.
    Everything the Shortcut will not log is dropped before any timestamp is
    formatted or any value converted.
    """
    authorise(query.get("token", ""))

    blob = extract_upload(body, content_type)
    if not blob:
        raise BadRequest(400, "no file in the request")
    if len(blob) > MAX_BYTES:
        raise BadRequest(413, "file too large")

    rows, source_unit = read_rows(blob)
    total = len(rows)

    # Apple Health is the cursor: the Shortcut sends its newest Blood Glucose
    # sample, so nearly every share collapses to a handful of rows here.
    cutoff = parse_since(query["since"]) if query.get("since") else None
    if cutoff:
        rows = [r for r in rows if r[0] > cutoff]

    every = positive_int(query, "every")
    if every:
        rows = thin(rows, every)

    limit = positive_int(query, "limit")
    if limit:
        rows = rows[-limit:]

    # Default to mg/dL because that is what the published Shortcut's Log Health
    # Sample action is set to. Anyone whose Shortcut uses mmol/L passes
    # ?unit=mmol/L; the two must agree or Health records a wrong number.
    out_unit = canonical_unit(query.get("unit") or DEFAULT_UNIT)

    # The Shortcut reads exactly two keys, and every extra byte is JSON the
    # phone has to parse before the loop can start. `verbose=1` restores the
    # full shape for anything else that wants it.
    verbose = str(query.get("verbose", "")).lower() in ("1", "true", "yes")
    readings = []
    for when, value in rows:
        reading = {
            "value": convert_value(value, source_unit, out_unit),
            # This exact shape is what Shortcuts' date detector parses, and it
            # works on a non-English device -- do not localise it.
            "date_text": when.strftime("%b %d, %Y at %I:%M %p"),
        }
        if verbose:
            reading["date_iso"] = when.isoformat(timespec="seconds")
            reading["unit"] = out_unit
        readings.append(reading)

    return readings, total, out_unit, source_unit, cutoff
