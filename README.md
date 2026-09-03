# ican-health-sync

Turn a Sibionics / iCan CGM `.xls` export into JSON that an Apple Shortcut can
write straight into Apple Health.

Share the export from the CGM app, tap the Shortcut, done. Readings land in
Health with their real measurement times.

## "I read that this is impossible"

Search results and forum answers commonly say the iCan / Sinocare CGM app has
no HealthKit support and no export, and that the only route is an Android phone
running xDrip+ or Juggluco feeding Nightscout, plus a native iOS app to write
HealthKit.

That is out of date, at least for the Thai-language iCan app on i3/i6:

- **The app does export.** Its share button hands over an `.xls` file.
- **You do not need Android, xDrip+, Nightscout, or Xcode.**
- **You do not need to write a native app.** Apple's own Shortcuts app is
  native and can write HealthKit — `Log Health Sample` is a built-in action.

The only genuinely missing piece is that Shortcuts cannot read `.xls`. That is
the single gap this project fills. Everything else is stock iOS.

Built and verified end to end on an iPhone: readings land in Apple Health with
their real measurement times, not the import time.

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

Run your own instance. Nobody operates this as a service — there is no
endpoint to share, and pointing your Shortcut at a stranger's deployment would
mean handing them your glucose data.

## Deploy your own

There is no shared instance and no service to sign up for. You deploy a copy,
it answers only to you, and it keeps nothing.

### Vercel (free, nothing to maintain)

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https://github.com/Nice0w0/ican-health-sync)

Click, connect your GitHub account, done. You get an HTTPS URL like
`https://your-project.vercel.app`. Every push redeploys automatically.

Then set two environment variables in the Vercel dashboard
(Settings → Environment Variables):

| Variable | Value |
|---|---|
| `CGM_TZ_OFFSET` | your UTC offset in hours, e.g. `7` for Bangkok |
| `CGM_TOKEN` | any long random string — required, since a Vercel URL is public |

Your endpoint is `https://your-project.vercel.app/api/convert`.

### Self-hosted

No dependencies at all, so plain Python works:

```bash
git clone https://github.com/Nice0w0/ican-health-sync.git && cd ican-health-sync
python3 server.py        # http://127.0.0.1:8000/api/convert
```

Or with Docker:

```bash
cp .env.example .env      # set CGM_TOKEN
docker compose up -d --build
```

The container binds to `127.0.0.1` only. Put a TLS proxy in front — glucose
readings should not cross the internet in plaintext. With Caddy:

```
cgm.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CGM_TOKEN` | *(unset)* | If set, requests must carry `X-Token` or `?token=`. **Set it on any public deployment.** |
| `CGM_TZ_OFFSET` | `7` | **The wearer's** UTC offset in hours. The export contains no timezone, so this is how local reading times are reconstructed — a server running in UTC still produces correct times. |

## API

### `POST /convert`

Body: the `.xls`, either as a multipart file field (any name) or as the raw
request body.

| Query | Meaning |
|---|---|
| `since` | ISO 8601 or a unix timestamp. Returns only readings **strictly newer**. A value without a timezone is read in `CGM_TZ_OFFSET`. |
| `every` | Thin to at most one reading per this many minutes. See [Speed](#speed). |
| `unit` | `mg/dL` (default) or `mmol/L`. Values are converted from whatever the export declares. |
| `limit` | At most this many readings, newest first. Useful for a first run. |
| `verbose` | `1` also returns `date_iso` and `unit` per reading. Off by default — the Shortcut does not read them and it doubles the payload. |
| `token` | Alternative to the `X-Token` header, for clients that cannot set headers easily. |

Returns a JSON array, oldest first — only what the Shortcut logs:

```json
[{"value": 72, "date_text": "Sep 01, 2026 at 07:46 PM"}]
```

`date_text` exists because Shortcuts' date detector parses that exact shape
reliably — including on a non-English device. Feed *that* to **Get Dates from
Input**.

Filtering happens before formatting: rows Health already has are dropped before
any timestamp is rendered or any value converted.

Response headers `X-Readings-Total`, `X-Readings-Returned`, `X-Unit`,
`X-Source-Unit` and `X-Since` let a client report what happened without walking
the array. `X-Since` echoes the cursor the server actually parsed, which is the
quick way to tell *"nothing new"* from *"the cursor never arrived"* — an empty
array looks the same either way.

### Speed

A CGM samples every three minutes, so one day is ~480 readings. The Shortcut
spends four on-device actions per reading, and that loop — not the network and
not this service — is what makes a big share slow.

`?every=15` returns one reading per 15 minutes instead, cutting the loop 5× and
the JSON with it. The curve in Health looks the same. The generated Shortcut
uses `every=15`; change the number in its URL, or drop the parameter to log
every single reading.

Thinning walks newest-first, so **the most recent reading is always kept** and
each one kept is at least that far apart.

### Units

The export declares its unit in the value column header, and the service
converts to whatever `?unit=` asks for — so the number returned always matches
the unit the Shortcut is configured to log.

This matters because Shortcuts' **Log Health Sample** takes its unit from a
fixed picker that cannot be driven by a variable. If the two disagree, Health
records a badly wrong number with no error: `7.2 mmol/L` written as
`7.2 mg/dL` reads as severe hypoglycaemia. Pinning both from one place is the
only way they cannot drift.

An unrecognised unit is rejected with `422` rather than assumed. Both English
and Thai spellings of mg/dL and mmol/L are understood.

Errors: `400` unreadable request, `401` bad token, `413` oversized,
`422` not a CGM export.

### `GET /healthz`

`{"ok": true}`.

## The Shortcut

`shortcut/iCan to Health (template).shortcut` is ready to import. It contains a
**placeholder URL and no token** — you point it at your own deployment.

1. Open the file on the iPhone and add the shortcut.
2. Open its **Get Contents of URL** action and replace the URL with your own:
   `https://your-project.vercel.app/api/convert?token=YOUR_TOKEN&unit=mg/dL&every=15&since=`
   Leave the date variable that already sits at the end of the field.
3. Turn on *Show in Share Sheet*, accepting files.

On macOS you can generate a filled-in copy instead:

```bash
python3 build_shortcut.py \
  --url https://your-project.vercel.app/api/convert \
  --token YOUR_TOKEN --unit mg/dL --every 15 -o mine.shortcut
shortcuts sign -m anyone -i mine.shortcut -o "iCan to Health.shortcut"
```

Pass `--unit mmol/L` if that is what your Health app should record; the flag
pins the URL parameter and the Log Health Sample picker together so they cannot
disagree. `--every 0` keeps every reading.

> **Never publish a filled-in shortcut.** The token is embedded in its URL, and
> anyone holding it can spend your deployment's quota. There is no stored data
> to steal and no way to write to your Health, but rotate the token in Vercel
> if one leaks.

### What it does, in order

1. **Find Health Samples** — Blood Glucose, newest first, limit 1 → the import cursor
2. **Get Dates from Input** → that sample's timestamp
3. **Get Contents of URL** — POST the shared `.xls`, with the cursor as `?since=`
4. **Get Dictionary from Input**
5. **Repeat with Each**
6. **Get Dictionary Value** — `value`
7. **Get Dictionary Value** — `date_text`
8. **Get Dates from Input**
9. **Log Health Sample** — Blood Glucose, value ← 6, date ← 8
10. **End Repeat**

Before the first real run, check that action 1 shows **Sort by Start Date,
Latest First, Limit 1**. Without that the cursor is wrong and readings import
repeatedly — and Health has no way to overwrite a sample, so duplicates have to
be deleted by hand.

Then add `&limit=1` to the URL for one run and confirm in Health that the
sample carries the right value **and** the CGM's measurement time rather than
the import time. Remove it once both check out.

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
- A full day is ~480 readings and `Repeat with Each` is slow in Shortcuts, so
  the Shortcut asks for `every=15`. Raise the resolution only if you are ready
  to wait — and import regularly rather than in one batch.

## Licence

MIT.
