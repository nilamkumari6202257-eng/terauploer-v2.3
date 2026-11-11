# TeraBox Uploader Telegram Bot

Telegram channel/group/user ka media download karke TeraBox par upload karta hai
aur shareable link deta hai. Is version me targets dashboard, resumable batch,
auto mode, story/post links, **user targets (stories + profile posts)** with
per-target story/post/auto mode switches, direct message-link handling aur
private content ke liye Telegram user session add kiya gaya hai.

## Features

- **`/set`** — target group/channel + start message save karo. Multiple targets.
  **User ID / @username / link** bhejo to wo user target ban jata hai.
- **User targets** — kisi user ko target karo (user ID, @username ya link).
  Har user target par 3 alag switches:
  - 📸 **Story Mode** — 24h wali stories
  - 🎬 **Post Mode** — profile ke *Posts* tab wale posts
  - 🤖 **Auto Mode** — nayi story/post khud download + TeraBox upload
  Buttons se kabhi bhi ON/OFF karo, `Download Stories` / `Download Posts` se
  purani cheezein abhi lo, aur `Links List` me sab links milo.
- **Story link paste** — `t.me/<user>/s/<id>` link bhejo, wo story download
  hoke TeraBox link ban jayegi.
- **`/bot`** — dusra bot track karo (ID / @username / t.me link). Jab wo bot
  video bhejega, automatically download + TeraBox upload hoti hai, live
  progress bar ke saath link bot chat me aata hai. `/bot list`,
  `/bot remove <id/@username>` se manage karo.
- **`/batch`** — start point se `100` / `500` / `1000` / `all` / custom messages
  process karo. FloodWait retry, cursor resume aur duplicate prevention.
- **Auto mode** — per-target ON/OFF. Nayi media automatically upload hoti hai
  aur TeraBox link bot chat me aata hai.
- **Direct link paste** — koi command nahi. `t.me/...` message/story link paste
  karo, link wapas milega (public aur private dono).
- **`/login` / `/logout`** — phone + OTP + 2FA se Telegram user session.
  Private groups/channels isi session se download hote hain.
- **Live progress** — `[██████░░░░] 60% | 12.3 MB/s | ETA: 42s`, 3–5 sec me ek edit.
- **`/targets`** — inline dashboard: kitne users targeted hain, sab dikhta hai.
  Status page, story/post/auto mode buttons, download buttons, links list
  (10 per page), TXT export. Sab navigation ek hi message edit karta hai.
- Parallel Telegram download (1 MiB ranges, resume) + parallel TeraBox upload
  (4 MiB parts, host failover).
- Encrypted SQLite storage for TeraBox cookies and Telegram session strings.
- **Web dashboard (v2.3)** — bot ke saath ek activity log website: har file
  upload aur network error live dikhta hai. Koi extra pip dependency nahi.

## Web Dashboard

Bot process ke andar hi ek chhota web server chalta hai jo activity log page
serve karta hai — **file uploads + network errors**, har 5 second me backend
(`/api/logs`) ko poll karke latest events.

- Page: `http://<host>:8080` (Docker me port mapping already hai)
- Filter: All / Uploads / Failures / Network errors, live counters ke saath
- Pause/Resume, manual refresh, naye events par highlight
- Sources tagged: direct / batch / auto / story / bot-watch / terabox / telegram
- Bot behaviour par zero asar — dashboard kabhi bot ko crash nahi kar sakta

```env
WEB_DASHBOARD_ENABLED=true     # default true
WEB_DASHBOARD_PORT=8080
WEB_DASHBOARD_TOKEN=secret123  # optional — phir /?token=secret123 se khulega
```

**Hosted website mode:** `WEB_DASHBOARD_WEBHOOK_URL` set karne par bot apne
saare activity events ek deployed TeraFast dashboard website ko bhi live push
kar deta hai (3s batching, failures par silent re-queue):

```env
WEB_DASHBOARD_WEBHOOK_URL=https://<dashboard-site>/api/events
WEB_DASHBOARD_WEBHOOK_TOKEN=<shared-secret>   # agar site par token set ho
```

`dashboard-site/` folder me hosted website ka source hai (Next.js 16 +
Prisma/SQLite). Wahi log display component wahi JSON shape poll karta hai.
Webhook URL us site ke dashboard me "Connect" section se copy ho sakta hai.

## Commands

| Command | Description |
| --- | --- |
| `/start` | Main menu |
| `/help` | Command reference |
| `/set` | Add target chat/user + start point (ID, @username, link) |
| `/targets` | Target dashboard — modes on/off, list, count |
| `/batch` | Bulk download + upload |
| `/bot` | Bot watch — dusre bot ki videos auto-upload |
| `/parallel` / `/pr` | Runtime max parallel jobs ko 1–4 set karo |
| `/login` | Telegram user session (phone/OTP/2FA) |
| `/logout` | Remove user session |
| `/bind` / `/change` | Bind TeraBox cookie |
| `/status` | TeraBox quota, session, targets |
| `/delete` | Delete TeraBox config |
| `/queue` | Queue status |
| `/stop` | Cancel your running jobs |
| `/cancel` | Abort current input flow |

## Setup

```bash
cp .env.example .env
# .env me API_ID, API_HASH, BOT_TOKEN bharo
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

`DATA_DIR` default `./data` rakho, taaki encryption key, SQLite DB aur downloads
persistent ek hi directory me rahen. First start par `DATA_DIR/.encryption_key`
restrictive permissions ke saath generate hoti hai. Existing encrypted tokens
ke liye wahi purani key preserve karna zaroori hai.

### TeraBox cookie

1. Browser me `dm.1024terabox.com` par login karo.
2. F12 → Network → koi request → **Cookie** header copy karo.
3. Bot me `/bind` karke poori cookie paste karo.

Cookie Fernet-encrypted SQLite me save hoti hai aur message turant delete hota hai.

### Private groups/channels

1. `/login` → phone number (`+919876543210`).
2. Telegram OTP code.
3. 2FA password (agar enabled ho).

Session string encrypted save hoti hai. `/logout` se delete hoti hai. Bot ko
public channel me add karna optional hai — user session fallback khud use hota hai.

## Docker

```bash
cp .env.example .env
# .env me secrets bharo
docker compose up -d --build
docker compose logs -f terabox-bot
```

Compose persistent named volume `/app/data` mount karta hai. Koi web port open nahi hota.

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `API_ID`, `API_HASH`, `BOT_TOKEN` | — | Telegram credentials (required) |
| `DATA_DIR` | `./data` | Persistent state directory |
| `ENCRYPTION_KEY` | auto | Fernet key for cookies/sessions |
| `MAX_PARALLEL` | `1` | FIFO queue workers |
| `TELEGRAM_DOWNLOAD_WORKERS` | `3` | Per-job download workers |
| `TELEGRAM_TOTAL_DOWNLOAD_WORKERS` | `6` | Global download stream cap |
| `TERABOX_UPLOAD_THREADS` | `6` | Per-job upload threads |
| `TERABOX_TOTAL_UPLOAD_THREADS` | `8` | Global upload part cap |
| `TERABOX_MAX_RETRIES` | `8` | Part upload retries |
| `MAX_FILE_MB` | `4096` | Telegram file size guard |
| `MIN_FREE_MB` | `512` | Minimum disk space kept free during downloads |
| `MAX_URL_GB` | `10` | yt-dlp download limit |
| `REMOTE_FOLDER` | `TG_Uploads` | TeraBox base folder |
| `REMOTE_FOLDER_PER_TARGET` | `true` | Per-target subfolder |
| `BATCH_CHUNK_SIZE` | `100` | Messages fetched per request |
| `BATCH_MAX_MESSAGES` | `0` | Hard cap per batch (0 = none) |
| `AUTO_RESUME_BATCH` | `true` | Resume running jobs on boot |
| `AUTO_POLL_SECONDS` | `20` | Auto mode poll interval |
| `POST_POLL_EVERY` | `3` | Profile posts har Nth auto poll par check hote hain |
| `PROGRESS_EDIT_INTERVAL` | `4` | Progress edit throttle |
| `LINKS_PAGE_SIZE` | `10` | Links per page |

## Data model

- `targets` — chat, start message, auto/story/post mode, last seen, counters.
- `uploads` — unique `(user_id, chat_id, message_id)`; duplicate upload impossible.
  Stories/posts bhi isi me store hoti hain (message_id = story id).
- `batch_jobs` — start/end/cursor and counters for resume.
- `terabox_tokens`, `user_sessions` — Fernet-encrypted blobs.

## Project layout

```
bot.py                  entry point
config.py               env configuration
db.py                   SQLite layer
terabox.py              TeraBox web client
tgdl.py                 parallel Telegram downloader
web/dashboard.html      built-in dashboard page (activity log)
handlers/               common, targets, batch, bots, login, direct, router
services/               pipeline, batch_engine, auto_engine, userbot, progress, story,
                        activity_log (event store), web_dashboard (HTTP server)
utils/                  formatting, links, queue, telegram, async helpers
tests/                  unit tests
dashboard-site/         hosted dashboard website source (Next.js, optional deploy)
```

## Checks

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
```

## Notes

- TeraBox ka official public upload API nahi hai; `terabox.py` website ke current
  web endpoints use karta hai. Website change hone par TeraBox-side update ki
  zaroorat pad sakti hai.
- Telegram bot/media route ki real size limit provider par depend karti hai.
  `MAX_FILE_MB` sirf local guard hai.
- Batch "N messages" ka matlab start message ID se N IDs ka range hai; deleted
  messages skip ho jaate hain.
