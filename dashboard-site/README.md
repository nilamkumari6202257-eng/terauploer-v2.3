# TeraFast Dashboard Website

Hosted version of the TeraFast activity dashboard — the same log display
component jo bot ke andar `web/dashboard.html` par serve hota hai, lekin ye
website hamesha-on reh sakti hai aur bot webhook se live events receive karti hai.

- **Page** (`/`): file uploads + network errors ka live log — backend
  (`/api/logs`) ko har 5 second me poll karta hai.
- **`GET /api/logs`**: latest events + stats (same JSON shape as the bot's
  built-in dashboard).
- **`POST /api/events`**: webhook ingest — bot ka
  `WEB_DASHBOARD_WEBHOOK_URL` yahan point karo.
- **`POST /api/events/seed`**: clearly-marked sample events (demo ke liye).
- **`DELETE /api/events`**: log clear (`?samples=1` = sirf sample events).

## Stack

Next.js 16 (App Router) · TypeScript · Tailwind CSS 4 · shadcn/ui · Prisma + SQLite

## Setup

```bash
cd dashboard-site
bun install        # ya npm install
cp .env.example .env
bun run db:push    # Prisma client + SQLite schema
bun run dev        # http://localhost:3000
```

Production: `bun run build && bun run start` (ya kisi bhi Node host par deploy karo).

## Env

| Variable | Description |
| --- | --- |
| `DATABASE_URL` | SQLite file path, e.g. `file:./db/custom.db` |
| `DASHBOARD_INGEST_TOKEN` | Optional shared secret. Set karo to bot ka `WEB_DASHBOARD_WEBHOOK_TOKEN` same hona chahiye. Khali = open ingest. |

## Bot ko connect karo

Bot ke `.env` me:

```env
WEB_DASHBOARD_ENABLED=true
WEB_DASHBOARD_WEBHOOK_URL=https://<this-site>/api/events
# WEB_DASHBOARD_WEBHOOK_TOKEN=<DASHBOARD_INGEST_TOKEN ka value>
```

Bot apne saare file uploads / network errors 3-second batches me push karega.
Website ke "Connect" section me ready-made URL copy button hai.
