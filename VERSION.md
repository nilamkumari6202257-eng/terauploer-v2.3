# Terafast Release

This repository contains the Terafast Telegram to TeraBox uploader configuration and runtime entry point.

Runtime secrets (`.env`, `data/`) kabhi archive me nahi jate — sirf `.env.example` included hai.

## v2.3 — Web dashboard (activity log)

- **Built-in web dashboard**: bot ke andar hi ek web page serve hota hai
  (`http://<host>:8080`) jo backend (`/api/logs`) ko har 5s poll karke
  **latest activity events — file uploads + network errors** dikhata hai.
  - Stat cards (uploads / failures / network errors / last activity)
  - Filter chips: All / Uploads / Failures / Network errors / Info
  - Pause/Resume + manual refresh, naye events par highlight
  - Sources tagged: direct / batch / auto / story / bot-watch / terabox / telegram
- **Zero new pip dependencies** — dashboard pure Python stdlib (http.server) par.
  `record_event()` kabhi raise nahi hota; dashboard ki wajah se bot crash nahi hota.
- **Webhook forwarding (optional)**: `WEB_DASHBOARD_WEBHOOK_URL` set karne par
  bot saare events hosted TeraFast dashboard website par live push karta hai.
- **Hosted website** (`dashboard-site/`): Next.js 16 + Prisma version of the same
  log display component, `/api/logs` poll + `/api/events` webhook ingest ke saath.
- Network errors me rate limiting (6 events / 10s burst window) — part-upload
  retry storm dashboard ko flood nahi karta.
- Docker: port 8080 exposed + compose mapping.
- Sab 24 purane tests pass + naye activity-log/dashboard tests.

## v2.2 — Stability fixes

- **Links List 0-links fix**: story/post uploads ab sahi `target_id` ke saath save hote
  hain (pehle NULL save hota tha, isliye list 0 dikhati thi). Purane uploads bhi
  automatically target se re-attach ho jate hain. Direct story links bhi ab
  matching target se attach hote hain.
- **TeraBox emoji path fix (errno -7)**: target title me emoji/special chars
  (jaise `Tinder18+ ❤️😍`) hone par upload fail hota tha. Ab:
  1. `safe_name()` emoji/symbols strip karta hai (`Tinder18+ ❤️😍` → `Tinder18`)
  2. Agar TeraBox path phir bhi reject kare, upload ek baar plain base folder +
     simple name se automatic retry hota hai — path ki wajah se kabhi hard-fail nahi
- **Bot-watch PEER_ID_INVALID fix**: fresh user session me watched bot ka numeric id
  resolve nahi hota tha. Ab username se server-side resolve + ek-baar peer cache
  warmup (dialog scan) + retry.
- **Download All (Start All) button**: stories + posts dono ek saath
- 24/24 unit tests pass

## v2.1 — Final full-featured release

- Story link support: `t.me/<user>/s/<id>` paste karo → download + TeraBox upload
- User targets: user ID / @username / link se kisi user ko target karo
- Per-target 3 mode switches (buttons se kabhi bhi change karo):
  - 📸 Story Mode — 24h stories
  - 🎬 Post Mode — profile ke Posts tab (pinned stories)
  - 🤖 Auto Mode — nayi story/post khud upload
- `/targets` dashboard: kitne users targeted hain, sab ka mode status, links list, TXT export
- Download Stories / Download Posts buttons — purani media ka backfill
- Auto engine: 20s poll, stories har cycle, posts har 3rd cycle
- Bot watch (`/bot`), resumable batch (`/batch`), direct links, yt-dlp URL download
- Telegram user session (`/login`) — private content + stories ke liye
- 24/24 unit tests pass
