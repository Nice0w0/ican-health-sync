"""
Minimal read-only .xls (OLE2 + BIFF8) reader -- Python standard library only.

Exists so the iPhone side needs no `pip install`: drop this next to the script
in a-Shell and it works. Scope is deliberately tiny -- enough to read a cell
grid out of a Sibionics iCan CGM export, nothing more. No formulas, no styles,
no dates, first worksheet only.

Anything it does not understand raises rather than returning a partial grid.
A silently dropped or misindexed cell would move a glucose reading onto the
wrong timestamp, which is worse than a crash.
"""

from __future__ import annotations

import struct

ENDOFCHAIN = 0xFFFFFFFE
FREESECT = 0xFFFFFFFF

# BIFF record types we handle.
BOF = 0x0809
EOF_REC = 0x000A
BOUNDSHEET = 0x0085
SST = 0x00FC
CONTINUE = 0x003C
LABELSST = 0x00FD
LABEL = 0x0204
RSTRING = 0x00D6
NUMBER = 0x0203
RK = 0x027E
MULRK = 0x00BD
BLANK = 0x0201
MULBLANK = 0x00BE
BOOLERR = 0x0205
FORMULA = 0x0006
STRING_REC = 0x0207

_CELL_RECORDS = {
    LABELSST, LABEL, RSTRING, NUMBER, RK, MULRK,
    BLANK, MULBLANK, BOOLERR, FORMULA,
}


class XlsError(Exception):
    pass


# --------------------------------------------------------------------------
# OLE2 compound file
# --------------------------------------------------------------------------

class _Ole:
    def __init__(self, data: bytes):
        if data[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            raise XlsError("not an OLE2 compound file (.xls)")
        self.data = data
        self.sector_size = 1 << struct.unpack_from("<H", data, 0x1E)[0]
        self.mini_size = 1 << struct.unpack_from("<H", data, 0x20)[0]
        self.mini_cutoff = struct.unpack_from("<I", data, 0x38)[0]

        num_fat = struct.unpack_from("<I", data, 0x2C)[0]
        dir_start = struct.unpack_from("<I", data, 0x30)[0]
        mini_fat_start = struct.unpack_from("<I", data, 0x3C)[0]
        num_mini_fat = struct.unpack_from("<I", data, 0x40)[0]
        difat_start = struct.unpack_from("<I", data, 0x44)[0]
        num_difat = struct.unpack_from("<I", data, 0x48)[0]

        # DIFAT: first 109 entries live in the header, the rest in a chain.
        difat = list(struct.unpack_from("<109I", data, 0x4C))
        sect = difat_start
        for _ in range(num_difat):
            if sect in (ENDOFCHAIN, FREESECT):
                break
            block = self._sector(sect)
            per = self.sector_size // 4 - 1
            difat.extend(struct.unpack_from("<%dI" % per, block, 0))
            sect = struct.unpack_from("<I", block, per * 4)[0]

        self.fat = []
        for s in difat[:num_fat]:
            if s in (ENDOFCHAIN, FREESECT):
                continue
            block = self._sector(s)
            self.fat.extend(struct.unpack_from("<%dI" % (self.sector_size // 4), block, 0))

        self.minifat = []
        sect = mini_fat_start
        for _ in range(num_mini_fat):
            if sect in (ENDOFCHAIN, FREESECT):
                break
            block = self._sector(sect)
            self.minifat.extend(struct.unpack_from("<%dI" % (self.sector_size // 4), block, 0))
            sect = self._next(sect)

        self.dir_entries = self._read_directory(dir_start)

        # The mini stream is stored as a normal stream hanging off the root entry.
        root = self.dir_entries[0]
        self.mini_stream = self._read_chain(root["start"], root["size"]) if root["size"] else b""

    def _sector(self, n: int) -> bytes:
        off = 512 + n * self.sector_size
        block = self.data[off:off + self.sector_size]
        if len(block) != self.sector_size:
            raise XlsError("truncated file: sector %d is out of range" % n)
        return block

    def _next(self, n: int) -> int:
        if n >= len(self.fat):
            raise XlsError("FAT chain runs past end of table")
        return self.fat[n]

    def _read_chain(self, start: int, size: int) -> bytes:
        out = bytearray()
        sect, guard = start, 0
        while sect not in (ENDOFCHAIN, FREESECT) and len(out) < size:
            out += self._sector(sect)
            sect = self._next(sect)
            guard += 1
            if guard > len(self.fat) + 1:
                raise XlsError("cyclic FAT chain")
        return bytes(out[:size])

    def _read_mini_chain(self, start: int, size: int) -> bytes:
        out = bytearray()
        sect, guard = start, 0
        while sect not in (ENDOFCHAIN, FREESECT) and len(out) < size:
            off = sect * self.mini_size
            out += self.mini_stream[off:off + self.mini_size]
            if sect >= len(self.minifat):
                raise XlsError("mini FAT chain runs past end of table")
            sect = self.minifat[sect]
            guard += 1
            if guard > len(self.minifat) + 1:
                raise XlsError("cyclic mini FAT chain")
        return bytes(out[:size])

    def _read_directory(self, start: int) -> list:
        raw = self._read_chain(start, 1 << 30)  # walk to end of chain
        entries = []
        for off in range(0, len(raw) - 127, 128):
            name_len = struct.unpack_from("<H", raw, off + 64)[0]
            if name_len < 2:
                continue
            name = raw[off:off + name_len - 2].decode("utf-16-le", "replace")
            entries.append({
                "name": name,
                "type": raw[off + 66],
                "start": struct.unpack_from("<I", raw, off + 116)[0],
                "size": struct.unpack_from("<I", raw, off + 120)[0],
            })
        if not entries:
            raise XlsError("empty OLE directory")
        return entries

    def stream(self, *names: str) -> bytes:
        for entry in self.dir_entries:
            if entry["name"] in names and entry["type"] == 2:
                if entry["size"] < self.mini_cutoff:
                    return self._read_mini_chain(entry["start"], entry["size"])
                return self._read_chain(entry["start"], entry["size"])
        raise XlsError("no %s stream found -- is this really an .xls file?"
                       % " or ".join(names))


# --------------------------------------------------------------------------
# BIFF records
# --------------------------------------------------------------------------

def _records(stream: bytes):
    """Yield (code, payload, offset). CONTINUE records are yielded as-is."""
    pos = 0
    n = len(stream)
    while pos + 4 <= n:
        code, length = struct.unpack_from("<HH", stream, pos)
        payload = stream[pos + 4:pos + 4 + length]
        if len(payload) != length:
            raise XlsError("truncated BIFF record 0x%04X at offset %d" % (code, pos))
        yield code, payload, pos
        pos += 4 + length


class _SstReader:
    """
    Reads the shared string table across SST + its CONTINUE records.

    The whole reason this class exists: a string may be split across a CONTINUE
    boundary, and the continuation restarts with a fresh grbit byte -- the
    encoding can flip from compressed to UTF-16 mid-string. Getting this wrong
    does not crash; it shifts every subsequent SST index, which would silently
    attach a glucose value to the wrong timestamp.
    """

    def __init__(self, blocks: list):
        self.blocks = blocks
        self.bi = 0
        self.off = 0

    def _ensure(self, n: int) -> None:
        """Move to the next block if fewer than n bytes remain in this one."""
        while self.bi < len(self.blocks) and len(self.blocks[self.bi]) - self.off < n:
            self.bi += 1
            self.off = 0

    def _take(self, n: int) -> bytes:
        self._ensure(n)
        if self.bi >= len(self.blocks):
            raise XlsError("shared string table ended early")
        out = self.blocks[self.bi][self.off:self.off + n]
        self.off += n
        return out

    def read(self) -> list:
        total, unique = struct.unpack_from("<ii", self._take(8), 0)
        if unique < 0:
            raise XlsError("negative shared string count")
        return [self._read_one() for _ in range(unique)]

    def _read_one(self) -> str:
        # cch + grbit are never split across a CONTINUE.
        self._ensure(3)
        cch = struct.unpack_from("<H", self._take(2))[0]
        grbit = self._take(1)[0]
        wide = bool(grbit & 0x01)
        rich = bool(grbit & 0x08)
        ext = bool(grbit & 0x04)
        n_runs = struct.unpack_from("<H", self._take(2))[0] if rich else 0
        n_ext = struct.unpack_from("<i", self._take(4))[0] if ext else 0

        chars = []
        left = cch
        while left:
            block = self.blocks[self.bi] if self.bi < len(self.blocks) else b""
            avail = len(block) - self.off
            if avail <= 0:
                # Crossing into a CONTINUE: one grbit byte, then the remainder.
                self.bi += 1
                self.off = 0
                if self.bi >= len(self.blocks):
                    raise XlsError("shared string ended early")
                wide = bool(self.blocks[self.bi][0] & 0x01)
                self.off = 1
                continue
            width = 2 if wide else 1
            take = min(left, avail // width)
            if take == 0:
                # One odd byte before the boundary; nothing usable here.
                self.off = len(block)
                continue
            raw = self.blocks[self.bi][self.off:self.off + take * width]
            self.off += take * width
            chars.append(raw.decode("utf-16-le" if wide else "latin-1"))
            left -= take

        for skip in (n_runs * 4, n_ext):
            left = skip
            while left > 0:
                block = self.blocks[self.bi] if self.bi < len(self.blocks) else b""
                avail = len(block) - self.off
                if avail <= 0:
                    self.bi += 1
                    self.off = 0
                    if self.bi >= len(self.blocks):
                        raise XlsError("shared string ended early")
                    continue
                step = min(left, avail)
                self.off += step
                left -= step
        return "".join(chars)


def _unicode_string(payload: bytes, pos: int) -> str:
    """XLUnicodeString as used inside LABEL -- never spans a CONTINUE here."""
    cch = struct.unpack_from("<H", payload, pos)[0]
    grbit = payload[pos + 2]
    pos += 3
    if grbit & 0x08:
        pos += 2
    if grbit & 0x04:
        pos += 4
    if grbit & 0x01:
        return payload[pos:pos + cch * 2].decode("utf-16-le")
    return payload[pos:pos + cch].decode("latin-1")


def _unrk(rk: int):
    if rk & 0x02:
        value = rk >> 2
        if value & 0x20000000:
            value -= 0x40000000
        value = float(value)
    else:
        value = struct.unpack("<d", struct.pack("<q", (rk & 0xFFFFFFFC) << 32))[0]
    return value / 100 if rk & 0x01 else value


def _clean(value):
    """Present whole floats as ints so 145.0 does not reach Health as '145.0'."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def read_grid(path) -> list:
    """Return the first worksheet as a list of rows of cell values ('' = empty)."""
    with open(path, "rb") as fh:
        stream = _Ole(fh.read()).stream("Workbook", "Book")

    records = list(_records(stream))

    # Globals substream: the SST, plus BOUNDSHEET offsets for each worksheet.
    strings, sheet_offsets = [], []
    for i, (code, payload, _) in enumerate(records):
        if code == BOUNDSHEET:
            sheet_offsets.append(struct.unpack_from("<I", payload, 0)[0])
        elif code == SST:
            blocks = [payload]
            for follow_code, follow_payload, _ in records[i + 1:]:
                if follow_code != CONTINUE:
                    break
                blocks.append(follow_payload)
            strings = _SstReader(blocks).read()
        elif code == EOF_REC:
            break

    if not sheet_offsets:
        raise XlsError("no worksheet found in workbook")

    start = sheet_offsets[0]
    cells = {}
    max_row = max_col = -1

    def put(row, col, value):
        nonlocal max_row, max_col
        cells[(row, col)] = value
        max_row = max(max_row, row)
        max_col = max(max_col, col)

    started = False
    for code, payload, offset in records:
        if offset < start:
            continue
        if code == BOF:
            if started:
                break  # next substream: another sheet, stop
            started = True
            continue
        if code == EOF_REC:
            break
        if code not in _CELL_RECORDS:
            continue

        row, col = struct.unpack_from("<HH", payload, 0)
        if code == LABELSST:
            index = struct.unpack_from("<I", payload, 6)[0]
            if index >= len(strings):
                raise XlsError("shared string index %d out of range (have %d) -- "
                               "the string table was misread" % (index, len(strings)))
            put(row, col, strings[index])
        elif code in (LABEL, RSTRING):
            put(row, col, _unicode_string(payload, 6))
        elif code == NUMBER:
            put(row, col, _clean(struct.unpack_from("<d", payload, 6)[0]))
        elif code == RK:
            put(row, col, _clean(_unrk(struct.unpack_from("<I", payload, 6)[0])))
        elif code == MULRK:
            count = (len(payload) - 6) // 6
            for k in range(count):
                rk = struct.unpack_from("<I", payload, 6 + k * 6 + 2)[0]
                put(row, col + k, _clean(_unrk(rk)))
        elif code == MULBLANK:
            for k in range((len(payload) - 6) // 2):
                put(row, col + k, "")
        elif code in (BLANK, BOOLERR, FORMULA, STRING_REC):
            put(row, col, "")

    if max_row < 0:
        return []
    return [[cells.get((r, c), "") for c in range(max_col + 1)]
            for r in range(max_row + 1)]
