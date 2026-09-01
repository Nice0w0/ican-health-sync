#!/usr/bin/env python3
"""
iCan / Sibionics CGM .xls -> JSON for Apple Health, as an HTTP endpoint.

Stateless on purpose. It parses the upload, returns the readings, and forgets
everything -- no database, no logs of values, nothing that would make whoever
runs it the custodian of someone else's medical data. De-duplication is the
caller's job, via `?since=`: the Shortcut asks Apple Health for its newest
Blood Glucose sample and passes that timestamp, so Health itself is the record
of what has already been imported.

    POST /convert            multipart file, or the raw .xls as the body
                             -> [{"date_iso", "date_text", "value", "unit"}, ...]
      ?since=<ISO 8601>      return only readings strictly newer than this
      ?limit=<n>             at most n readings, newest first
    GET  /healthz            liveness probe

Auth is optional: set CGM_TOKEN to require `X-Token` (or `?token=`). Leave it
unset for a private instance behind a VPN or on localhost.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from xlsmini import XlsError, read_grid

TOKEN = os.environ.get("CGM_TOKEN", "")
# The export carries no timezone; it is written in the wearer's local time, so
# the server must not assume its own -- a VPS is usually UTC.
TZ = timezone(timedelta(hours=float(os.environ.get("CGM_TZ_OFFSET", "7"))))

HEADER_COL0 = "เลขที่"
TIME_FORMAT = "%H:%M,%m/%d/%Y"  # "15:22,09/01/2026" -> 1 Sep 2026, 15:22
DEFAULT_UNIT = "mg/dL"
MAX_BYTES = 10 * 1024 * 1024

app = FastAPI(title="CGM export -> Apple Health", docs_url=None, redoc_url=None)


def check(token_header: str | None, token_query: str | None) -> None:
    if not TOKEN:
        return  # unauthenticated by choice; document it, do not fail closed here
    supplied = token_header or token_query or ""
    # Constant-time-ish compare so the token cannot be probed byte by byte.
    if len(supplied) != len(TOKEN) or not all(
            a == b for a, b in zip(supplied, TOKEN)):
        raise HTTPException(401, "bad token")


def parse_export(blob: bytes) -> tuple[list[dict], str]:
    with NamedTemporaryFile(suffix=".xls", delete=True) as fh:
        fh.write(blob)
        fh.flush()
        grid = read_grid(Path(fh.name))

    header_row = None
    for i, row in enumerate(grid):
        if row and str(row[0]).strip() == HEADER_COL0:
            header_row = i
            break
    if header_row is None:
        raise XlsError("no '%s' header row -- not a CGM export" % HEADER_COL0)

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
            raise XlsError("row %d: unexpected time %r" % (i + 1, raw_time))
        try:
            value = float(raw_value)
        except ValueError:
            raise XlsError("row %d: value %r is not a number" % (i + 1, raw_value))
        readings.append({
            "date_iso": when.isoformat(timespec="seconds"),
            # This exact shape is what Shortcuts' date detector parses, and it
            # works on a Thai-locale phone -- do not localise it.
            "date_text": when.strftime("%b %d, %Y at %I:%M %p"),
            "value": int(value) if value.is_integer() else value,
            "unit": unit,
        })
    if not readings:
        raise XlsError("header found but no readings under it")
    readings.sort(key=lambda r: r["date_iso"])
    return readings, unit


def parse_since(raw: str) -> datetime:
    """Accept what Shortcuts is likely to send, not just strict ISO."""
    text = raw.strip().replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        try:
            when = datetime.fromtimestamp(float(text), TZ)
        except ValueError:
            raise HTTPException(400, "cannot read since=%r; use ISO 8601 or a "
                                     "unix timestamp" % raw)
    # A naive timestamp means the caller's local clock, which is the wearer's.
    return when.replace(tzinfo=TZ) if when.tzinfo is None else when


async def read_upload(request: Request) -> bytes:
    """
    Shortcuts sends either a multipart form or the raw file as the body,
    depending on how "Get Contents of URL" is configured, and the field name is
    not predictable. Accept every shape.

    Done by hand because declaring an `UploadFile = File(...)` parameter makes
    FastAPI consume the stream while parsing the form, after which the
    raw-body branch dies with "Stream consumed".
    """
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        for value in form.values():
            if hasattr(value, "read"):        # any file field, whatever its name
                return await value.read()
        raise HTTPException(400, "multipart request carried no file")
    return await request.body()


@app.get("/healthz")
def liveness():
    return {"ok": True}


@app.post("/convert")
async def convert(request: Request,
                  x_token: str | None = Header(default=None),
                  token: str | None = Query(default=None),
                  since: str | None = Query(default=None),
                  limit: int | None = Query(default=None)):
    check(x_token, token)

    blob = await read_upload(request)
    if not blob:
        raise HTTPException(400, "no file in the request")
    if len(blob) > MAX_BYTES:
        raise HTTPException(413, "file too large")

    try:
        readings, unit = parse_export(blob)
    except XlsError as exc:
        raise HTTPException(422, str(exc))

    total = len(readings)
    if since:
        cutoff = parse_since(since)
        readings = [r for r in readings
                    if datetime.fromisoformat(r["date_iso"]) > cutoff]
    if limit is not None:
        readings = readings[-limit:]

    return JSONResponse(readings, headers={
        "X-Readings-Total": str(total),
        "X-Readings-Returned": str(len(readings)),
        "X-Unit": unit,
    })
