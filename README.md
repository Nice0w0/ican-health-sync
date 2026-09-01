# ican-health-sync

Turn a Sibionics / iCan CGM `.xls` export into JSON that an Apple Shortcut can
write straight into Apple Health.

Share the export from the CGM app, tap the Shortcut, done. Readings land in
Health with their real measurement times.

## Why this exists

Apple Health can only be written from iOS, and the Shortcuts app cannot read
`.xls` — the export is a real OLE2/BIFF8 binary, not a spreadsheet Shortcuts
understands. Something has to do the conversion. This is that something: a
small HTTP endpoint you run yourself.

## It stores nothing

No database, no accounts, no logging of readings. The service parses the
upload, returns the readings, and forgets. Whoever runs it never becomes the
custodian of anyone's medical data.

De-duplication is handled by **Apple Health itself**: the Shortcut asks Health
for its newest Blood Glucose sample and sends that timestamp as `?since=`, so
only genuinely new readings come back. HealthKit has no upsert — `Log Health
Sample` only ever appends — so this filtering has to happen before logging.

Run your own instance. Do not point your Shortcut at someone else's.

## Deploy

```bash
git clone https://github.com/Nice0w0/ican-health-sync.git && cd ican-health-sync
cp .env.example .env      # optional: set CGM_TOKEN
docker compose up -d --build
curl localhost:8000/healthz          # {"ok":true}
```

The container binds to `127.0.0.1` only. Put a TLS proxy in front — glucose
readings should not cross the internet in plaintext. With Caddy that is:

```
cgm.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CGM_TOKEN` | *(unset)* | If set, requests must carry `X-Token` or `?token=`. Leave unset only on a private network. |
| `CGM_TZ_OFFSET` | `7` | **The wearer's** UTC offset in hours. The export contains no timezone, so this is how local reading times are reconstructed. A server in UTC with the default `7` would still be correct for a wearer in Bangkok. |

## API

### `POST /convert`

Body: the `.xls`, either as a multipart file field (any name) or as the raw
request body.

| Query | Meaning |
|---|---|
| `since` | ISO 8601 or a unix timestamp. Returns only readings **strictly newer**. A value without a timezone is read in `CGM_TZ_OFFSET`. |
| `limit` | At most this many readings, newest first. Useful for a first run. |
| `token` | Alternative to the `X-Token` header, for clients that cannot set headers easily. |

Returns a JSON array, oldest first:

```json
[{"date_iso":  "2026-09-01T19:46:00+07:00",
  "date_text": "Sep 01, 2026 at 07:46 PM",
  "value":     72,
  "unit":      "mg/dL"}]
```

`date_text` exists because Shortcuts' date detector parses that exact shape
reliably — including on a non-English device. Feed *that* to **Get Dates from
Input**, not `date_iso`.

Response headers `X-Readings-Total`, `X-Readings-Returned` and `X-Unit` let a
client report what happened without walking the array.

Errors: `400` unreadable request, `401` bad token, `413` oversized,
`422` not a CGM export.

### `GET /healthz`

`{"ok": true}`.

## The Shortcut

Nine actions, all built in to iOS:

1. **Find Health Samples** — Blood Glucose, newest first, limit 1
2. **Get Contents of URL** — `POST https://your.host/convert?since=<step 1's date>`,
   request body **File** = Shortcut Input
3. **Get Dictionary from Input**
4. **Repeat with Each**
5. **Get Dictionary Value** — `value`, from Repeat Item
6. **Get Dictionary Value** — `date_text`, from Repeat Item
7. **Get Dates from Input** — step 6
8. **Log Health Sample** — Blood Glucose, mg/dL, value ← 5, date ← 7
9. **End Repeat**

Turn on *Show in Share Sheet* and accept files, so the CGM app's share button
offers it.

On the first run, add `&limit=1` to the URL and check in Health that the sample
reads the right value **and** carries the CGM's measurement time rather than
the import time. Then remove it.

## The `.xls` reader

`xlsmini.py` is a standalone OLE2 + BIFF8 reader in the standard library only —
no `xlrd`, no `pandas`. It handles both the mini-stream (small exports) and the
regular FAT chain, and is verified cell-for-cell against `xlrd` on real
exports.

The subtle part is the shared string table spanning `CONTINUE` records, where
the encoding can flip between compressed and UTF-16 mid-string. Getting that
wrong does not crash — it shifts every subsequent string index, which would
attach a glucose value to the wrong timestamp. If you change the reader, diff
the full grid against `xlrd` on real files before trusting it.

## Limitations

- Written against Sibionics/iCan Thai-language exports: it locates the row
  whose first cell is `เลขที่` and parses times as `%H:%M,%m/%d/%Y`. Other
  exporters will need adjusting.
- Blood glucose only.
- A full day is ~480 readings; a `Repeat with Each` that long is slow in
  Shortcuts. Import regularly rather than in one batch.

## Licence

MIT.
