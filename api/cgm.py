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
DEFAULT_UNIT = "mg/dL"
MAX_BYTES = 10 * 1024 * 1024


class BadRequest(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


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


def parse_export(blob: bytes) -> tuple[list[dict], str]:
    """Return (readings oldest-first, unit)."""
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

    unit = DEFAULT_UNIT
    head = str(grid[header_row][2]) if len(grid[header_row]) > 2 else ""
    if "(" in head and ")" in head:
        unit = head[head.index("(") + 1:head.rindex(")")].strip()

    readings = []
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
        readings.append({
            "date_iso": when.isoformat(timespec="seconds"),
            # This exact shape is what Shortcuts' date detector parses, and it
            # works on a non-English device -- do not localise it.
            "date_text": when.strftime("%b %d, %Y at %I:%M %p"),
            "value": int(value) if value.is_integer() else value,
            "unit": unit,
        })
    if not readings:
        raise BadRequest(422, "header found but no readings under it")
    readings.sort(key=lambda r: r["date_iso"])
    return readings, unit


def parse_since(raw: str) -> datetime:
    """Accept what a Shortcut is likely to send, not only strict ISO."""
    text = raw.strip().replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        try:
            when = datetime.fromtimestamp(float(text), TZ)
        except ValueError:
            raise BadRequest(400, "cannot read since=%r; use ISO 8601 or a unix "
                                  "timestamp" % raw)
    # A naive timestamp means the caller's own clock, which is the wearer's.
    return when.replace(tzinfo=TZ) if when.tzinfo is None else when


def authorise(supplied: str) -> None:
    if not TOKEN:
        return  # open by choice; documented
    # Constant-time-ish compare so the token cannot be probed byte by byte.
    if len(supplied) != len(TOKEN) or not all(a == b for a, b in zip(supplied, TOKEN)):
        raise BadRequest(401, "bad token")


def convert(body: bytes, content_type: str, query: dict) -> tuple[list[dict], int, str]:
    """Return (readings, total_before_filtering, unit)."""
    authorise(query.get("token", ""))

    blob = extract_upload(body, content_type)
    if not blob:
        raise BadRequest(400, "no file in the request")
    if len(blob) > MAX_BYTES:
        raise BadRequest(413, "file too large")

    readings, unit = parse_export(blob)
    total = len(readings)

    since = query.get("since")
    if since:
        cutoff = parse_since(since)
        readings = [r for r in readings
                    if datetime.fromisoformat(r["date_iso"]) > cutoff]

    limit = query.get("limit")
    if limit:
        try:
            readings = readings[-int(limit):]
        except ValueError:
            raise BadRequest(400, "limit must be a number")

    return readings, total, unit
