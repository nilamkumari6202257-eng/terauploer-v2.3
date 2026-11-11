/**
 * Shared activity-event types and helpers for the TeraFast dashboard.
 *
 * The SAME JSON shape is produced by:
 *  - this Next.js backend (Prisma/SQLite)          -> /api/logs
 *  - the bot's built-in dashboard (services/web_dashboard.py) -> /api/logs
 * so the log display component works against either backend unchanged.
 */

export type EventType = "upload" | "upload_failed" | "network_error" | "info";

export interface ActivityEvent {
  id: number;
  type: EventType;
  message: string;
  file_name: string | null;
  file_size: number | null;
  link: string | null;
  source: string | null;
  target: string | null;
  duration_sec: number | null;
  error: string | null;
  sample: boolean;
  created_at: string; // ISO-8601
}

export interface EventStats {
  uploads: number;
  upload_failed: number;
  network_errors: number;
  infos: number;
  total: number;
  last_event_at: string | null;
}

export interface LogsResponse {
  ok: boolean;
  events: ActivityEvent[];
  stats: EventStats;
  server_time: string;
}

export const EVENT_TYPES: EventType[] = [
  "upload",
  "upload_failed",
  "network_error",
  "info",
];

export function isEventType(value: unknown): value is EventType {
  return typeof value === "string" && (EVENT_TYPES as string[]).includes(value);
}

/** Ingest payload accepted by POST /api/events (matches the bot's forwarder). */
export interface IngestEvent {
  type: EventType;
  message: string;
  file_name?: string | null;
  file_size?: number | null;
  link?: string | null;
  source?: string | null;
  target?: string | null;
  duration_sec?: number | null;
  error?: string | null;
  created_at?: string | null;
}

export function sanitizeIngestEvent(raw: unknown): IngestEvent | null {
  if (!raw || typeof raw !== "object") return null;
  const e = raw as Record<string, unknown>;
  if (!isEventType(e.type)) return null;
  const message = typeof e.message === "string" ? e.message.slice(0, 500) : "";
  if (!message.trim()) return null;
  const str = (v: unknown, max = 500): string | null =>
    typeof v === "string" && v.trim() ? v.slice(0, max) : null;
  const num = (v: unknown): number | null => {
    if (v === null || v === undefined) return null;
    const n = typeof v === "number" ? v : Number(v);
    return Number.isFinite(n) ? n : null;
  };
  let createdAt: Date | null = null;
  if (typeof e.created_at === "string") {
    const d = new Date(e.created_at);
    if (!Number.isNaN(d.getTime())) createdAt = d;
  }
  return {
    type: e.type,
    message,
    file_name: str(e.file_name, 300),
    file_size: num(e.file_size),
    link: str(e.link, 1000),
    source: str(e.source, 60),
    target: str(e.target, 200),
    duration_sec: num(e.duration_sec),
    error: str(e.error, 1000),
    created_at: createdAt ? createdAt.toISOString() : null,
  };
}
