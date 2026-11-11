"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import {
  Activity,
  AlertTriangle,
  Check,
  ChevronDown,
  CloudUpload,
  Copy,
  FileX2,
  Info,
  Pause,
  Play,
  Radio,
  RefreshCw,
  Trash2,
  WifiOff,
  Zap,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  ActivityEvent,
  EventStats,
  EventType,
  LogsResponse,
} from "@/lib/events";

const POLL_INTERVAL_MS = 5000;

type Filter = "all" | EventType;

const FILTERS: { key: Filter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "upload", label: "Uploads" },
  { key: "upload_failed", label: "Failures" },
  { key: "network_error", label: "Network errors" },
  { key: "info", label: "Info" },
];

const TYPE_CONFIG: Record<
  EventType,
  {
    label: string;
    icon: typeof CloudUpload;
    box: string;
    text: string;
    chip: string;
    dot: string;
  }
> = {
  upload: {
    label: "Upload",
    icon: CloudUpload,
    box: "bg-emerald-500/15 text-emerald-400 ring-1 ring-emerald-500/30",
    text: "text-emerald-400",
    chip: "bg-emerald-500/10 text-emerald-300 ring-1 ring-emerald-500/30",
    dot: "bg-emerald-400",
  },
  upload_failed: {
    label: "Upload failed",
    icon: FileX2,
    box: "bg-amber-500/15 text-amber-400 ring-1 ring-amber-500/30",
    text: "text-amber-400",
    chip: "bg-amber-500/10 text-amber-300 ring-1 ring-amber-500/30",
    dot: "bg-amber-400",
  },
  network_error: {
    label: "Network error",
    icon: WifiOff,
    box: "bg-red-500/15 text-red-400 ring-1 ring-red-500/30",
    text: "text-red-400",
    chip: "bg-red-500/10 text-red-300 ring-1 ring-red-500/30",
    dot: "bg-red-400",
  },
  info: {
    label: "Info",
    icon: Info,
    box: "bg-slate-500/15 text-slate-300 ring-1 ring-slate-500/30",
    text: "text-slate-300",
    chip: "bg-slate-500/10 text-slate-300 ring-1 ring-slate-500/30",
    dot: "bg-slate-400",
  },
};

function timeAgo(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 10) return "just now";
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function humanSize(bytes: number | null | undefined): string {
  if (!bytes || bytes <= 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 100 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

function fullTime(iso: string): string {
  return new Date(iso).toLocaleString();
}

export default function DashboardPage() {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [stats, setStats] = useState<EventStats | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [live, setLive] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [flashIds, setFlashIds] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState<string | null>(null);
  const [origin, setOrigin] = useState("");
  const [setupOpen, setSetupOpen] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);
  const prevMaxId = useRef(0);

  useEffect(() => {
    setOrigin(window.location.origin);
  }, []);

  const fetchLogs = useCallback(
    async (silent: boolean = false) => {
      if (!silent) setLoading(true);
      try {
        const params = new URLSearchParams({ limit: "100" });
        if (filter !== "all") params.set("type", filter);
        const res = await fetch(`/api/logs?${params.toString()}`, {
          cache: "no-store",
        });
        if (!res.ok) throw new Error(`backend responded ${res.status}`);
        const data: LogsResponse = await res.json();
        if (!data.ok) throw new Error("backend error");
        const maxId = data.events.reduce((m, e) => Math.max(m, e.id), 0);
        if (prevMaxId.current > 0 && maxId > prevMaxId.current) {
          setFlashIds(
            new Set(
              data.events
                .filter((e) => e.id > prevMaxId.current)
                .map((e) => e.id)
            )
          );
        } else {
          setFlashIds(new Set());
        }
        prevMaxId.current = Math.max(prevMaxId.current, maxId);
        setEvents(data.events);
        setStats(data.stats);
        setError(null);
        setLastUpdated(new Date());
      } catch (err) {
        setError(err instanceof Error ? err.message : "connection failed");
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [filter]
  );

  useEffect(() => {
    prevMaxId.current = 0;
    fetchLogs(false);
  }, [fetchLogs]);

  useEffect(() => {
    if (!live) return;
    const id = setInterval(() => fetchLogs(true), POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [live, fetchLogs]);

  const runAction = useCallback(
    async (key: string, fn: () => Promise<Response>) => {
      setBusy(key);
      try {
        await fn();
        await fetchLogs(false);
      } catch {
        /* fetchLogs reports errors */
      } finally {
        setBusy(null);
      }
    },
    [fetchLogs]
  );

  const loadSamples = () =>
    runAction("seed", () =>
      fetch("/api/events/seed", { method: "POST" })
    );

  const removeSamples = () =>
    runAction("samples", () =>
      fetch("/api/events?samples=1", { method: "DELETE" })
    );

  const clearAll = () => {
    if (
      window.confirm(
        "Saare log events delete kar dein? Ye wapas nahi aayenge."
      )
    ) {
      runAction("clear", () => fetch("/api/events", { method: "DELETE" }));
    }
  };

  const copyText = async (key: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(key);
      setTimeout(() => setCopied(null), 1500);
    } catch {
      /* clipboard unavailable */
    }
  };

  const hasSamples = events.some((e) => e.sample);
  const status: { label: string; cls: string; pulse: boolean } = error
    ? {
        label: "Reconnecting",
        cls: "bg-red-500/10 text-red-300 ring-red-500/40",
        pulse: true,
      }
    : live
      ? {
          label: "Live",
          cls: "bg-emerald-500/10 text-emerald-300 ring-emerald-500/40",
          pulse: true,
        }
      : {
          label: "Paused",
          cls: "bg-slate-500/10 text-slate-300 ring-slate-500/40",
          pulse: false,
        };

  const statCards = [
    {
      key: "uploads",
      label: "Uploads",
      icon: CloudUpload,
      value: stats ? String(stats.uploads) : "—",
      accent: "text-emerald-400",
      ring: "ring-emerald-500/20",
    },
    {
      key: "upload_failed",
      label: "Upload failures",
      icon: FileX2,
      value: stats ? String(stats.upload_failed) : "—",
      accent: "text-amber-400",
      ring: "ring-amber-500/20",
    },
    {
      key: "network_errors",
      label: "Network errors",
      icon: WifiOff,
      value: stats ? String(stats.network_errors) : "—",
      accent: "text-red-400",
      ring: "ring-red-500/20",
    },
    {
      key: "last_activity",
      label: "Last activity",
      icon: Activity,
      value: stats?.last_event_at ? timeAgo(stats.last_event_at) : "—",
      accent: "text-slate-100",
      ring: "ring-slate-500/20",
    },
  ];

  const filterCounts: Record<Filter, number | null> = {
    all: stats?.total ?? null,
    upload: stats?.uploads ?? null,
    upload_failed: stats?.upload_failed ?? null,
    network_error: stats?.network_errors ?? null,
    info: stats?.infos ?? null,
  };

  return (
    <div className="min-h-screen flex flex-col bg-slate-950 text-slate-100">
      <main className="flex-1 w-full max-w-5xl mx-auto px-4 sm:px-6 pb-10">
        {/* Header */}
        <header className="pt-8 pb-6">
          <div className="flex flex-wrap items-center gap-4 justify-between">
            <div className="flex items-center gap-3">
              <div
                className="flex h-11 w-11 items-center justify-center rounded-xl bg-emerald-500/15 ring-1 ring-emerald-500/40"
                aria-hidden="true"
              >
                <Zap className="h-6 w-6 text-emerald-400" />
              </div>
              <div>
                <h1 className="text-xl sm:text-2xl font-semibold tracking-tight">
                  TeraFast · Activity Dashboard
                </h1>
                <p className="text-sm text-slate-400">
                  Telegram → TeraBox uploader ka live activity log
                </p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <span
                className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium ring-1 ${status.cls}`}
                role="status"
                aria-label={`Polling status: ${status.label}`}
              >
                <span
                  className={`h-1.5 w-1.5 rounded-full ${status.dot || "bg-current"} ${
                    status.pulse ? "animate-pulse" : ""
                  }`}
                  aria-hidden="true"
                />
                {status.label}
              </span>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setLive((v) => !v)}
                aria-label={live ? "Pause polling" : "Resume polling"}
                className="border-slate-700 bg-slate-900 text-slate-200 hover:bg-slate-800 hover:text-white"
              >
                {live ? (
                  <Pause className="h-4 w-4" />
                ) : (
                  <Play className="h-4 w-4" />
                )}
                <span className="hidden sm:inline">
                  {live ? "Pause" : "Resume"}
                </span>
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => fetchLogs(false)}
                disabled={loading}
                aria-label="Refresh logs now"
                className="border-slate-700 bg-slate-900 text-slate-200 hover:bg-slate-800 hover:text-white"
              >
                <RefreshCw
                  className={`h-4 w-4 ${loading ? "animate-spin" : ""}`}
                />
                <span className="hidden sm:inline">Refresh</span>
              </Button>
            </div>
          </div>
          <p className="mt-2 text-xs text-slate-500">
            {error
              ? `Backend se connect nahi ho pa raha (${error}) — retry every ${POLL_INTERVAL_MS / 1000}s`
              : lastUpdated
                ? `Last updated ${timeAgo(lastUpdated.toISOString())} · polling every ${POLL_INTERVAL_MS / 1000}s`
                : "Loading…"}
          </p>
        </header>

        {/* Stat cards */}
        <section
          aria-label="Activity summary"
          className="grid grid-cols-2 lg:grid-cols-4 gap-3 sm:gap-4"
        >
          {statCards.map((card) => (
            <div
              key={card.key}
              className={`rounded-xl bg-slate-900/70 ring-1 ${card.ring} p-4`}
            >
              <div className="flex items-center gap-2 text-xs text-slate-400">
                <card.icon className={`h-4 w-4 ${card.accent}`} aria-hidden="true" />
                {card.label}
              </div>
              <p className={`mt-2 text-2xl font-semibold ${card.accent}`}>
                {card.value}
              </p>
            </div>
          ))}
        </section>

        {/* Filters */}
        <section aria-label="Log filters" className="mt-6">
          <div className="flex flex-wrap items-center gap-2">
            {FILTERS.map((f) => {
              const active = filter === f.key;
              const count = filterCounts[f.key];
              return (
                <button
                  key={f.key}
                  onClick={() => setFilter(f.key)}
                  aria-pressed={active}
                  className={`inline-flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-xs font-medium ring-1 transition-colors ${
                    active
                      ? "bg-emerald-500/15 text-emerald-300 ring-emerald-500/40"
                      : "bg-slate-900 text-slate-400 ring-slate-700 hover:bg-slate-800 hover:text-slate-200"
                  }`}
                >
                  {f.label}
                  {count !== null && count > 0 && (
                    <span
                      className={`rounded-full px-1.5 text-[10px] ${
                        active
                          ? "bg-emerald-500/20 text-emerald-200"
                          : "bg-slate-800 text-slate-400"
                      }`}
                    >
                      {count}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        </section>

        {/* Log feed */}
        <section
          aria-label="Activity log"
          aria-live="polite"
          className="mt-4 rounded-xl bg-slate-900/70 ring-1 ring-slate-800"
        >
          <div className="flex items-center justify-between border-b border-slate-800 px-4 py-3">
            <div className="flex items-center gap-2 text-sm font-medium text-slate-200">
              <Radio className="h-4 w-4 text-emerald-400" aria-hidden="true" />
              Latest activity
            </div>
            <div className="flex items-center gap-2">
              {hasSamples && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={removeSamples}
                  disabled={busy === "samples"}
                  className="h-7 text-[11px] border-slate-700 bg-slate-900 text-slate-300 hover:bg-slate-800"
                >
                  Remove samples
                </Button>
              )}
              <Button
                variant="outline"
                size="sm"
                onClick={clearAll}
                disabled={busy === "clear" || !stats?.total}
                className="h-7 text-[11px] border-red-500/30 bg-slate-900 text-red-300 hover:bg-red-500/10"
              >
                <Trash2 className="h-3.5 w-3.5" />
                Clear
              </Button>
            </div>
          </div>

          {loading && events.length === 0 ? (
            <ul className="divide-y divide-slate-800/70 px-4">
              {[...Array(4)].map((_, i) => (
                <li
                  key={i}
                  className="flex items-center gap-3 py-4 animate-pulse"
                >
                  <div className="h-9 w-9 rounded-lg bg-slate-800" />
                  <div className="flex-1 space-y-2">
                    <div className="h-3 w-2/3 rounded bg-slate-800" />
                    <div className="h-3 w-1/3 rounded bg-slate-800/70" />
                  </div>
                </li>
              ))}
            </ul>
          ) : events.length === 0 ? (
            <div className="flex flex-col items-center gap-3 px-6 py-14 text-center">
              <AlertTriangle
                className="h-8 w-8 text-slate-500"
                aria-hidden="true"
              />
              <p className="text-sm text-slate-300">
                {error
                  ? "Backend reachable nahi hai — bot/local server chalu hai?"
                  : filter === "all"
                    ? "Abhi koi activity nahi. Bot connect karo ya demo ke liye sample data load karo."
                    : "Is filter me koi event nahi mila."}
              </p>
              {!error && filter === "all" && (
                <Button
                  size="sm"
                  onClick={loadSamples}
                  disabled={busy === "seed"}
                  className="bg-emerald-500 text-emerald-950 hover:bg-emerald-400"
                >
                  {busy === "seed" ? "Loading…" : "Load sample data"}
                </Button>
              )}
            </div>
          ) : (
            <ul className="divide-y divide-slate-800/70 max-h-[28rem] overflow-y-auto [&::-webkit-scrollbar]:w-1.5 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-slate-700 [&::-webkit-scrollbar-track]:bg-transparent">
              {events.map((event) => {
                const config = TYPE_CONFIG[event.type];
                const Icon = config.icon;
                const flash = flashIds.has(event.id);
                const size = humanSize(event.file_size);
                return (
                  <motion.li
                    key={event.id}
                    initial={{ opacity: 0, y: -4 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: 0.25 }}
                    className={`flex items-start gap-3 px-4 py-3 transition-colors ${
                      flash ? "bg-emerald-500/[0.07]" : ""
                    }`}
                  >
                    <div
                      className={`mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ${config.box}`}
                      aria-hidden="true"
                    >
                      <Icon className="h-4.5 w-4.5" />
                    </div>
                    <div className="min-w-0 flex-1">
                      <p className="text-sm text-slate-100 break-words">
                        {event.message}
                      </p>
                      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-slate-400">
                        <span
                          className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 font-medium ${config.chip}`}
                        >
                          <span
                            className={`h-1 w-1 rounded-full ${config.dot}`}
                            aria-hidden="true"
                          />
                          {config.label}
                        </span>
                        {event.file_name && (
                          <span className="truncate max-w-[14rem] font-mono">
                            {event.file_name}
                          </span>
                        )}
                        {size && <span>{size}</span>}
                        {event.target && <span>· {event.target}</span>}
                        {event.duration_sec != null &&
                          event.duration_sec > 0 && (
                            <span>· {event.duration_sec.toFixed(0)}s</span>
                          )}
                        {event.source && (
                          <span className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-300">
                            {event.sample ? "sample" : event.source}
                          </span>
                        )}
                        {event.link && (
                          <a
                            href={event.link}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex items-center gap-1 text-emerald-400 underline-offset-2 hover:underline"
                          >
                            TeraBox link ↗
                            <span className="sr-only">
                              (opens in new tab)
                            </span>
                          </a>
                        )}
                      </div>
                      {event.error && (
                        <p
                          className="mt-1 line-clamp-2 break-words font-mono text-[11px] text-red-300/80"
                          title={event.error}
                        >
                          {event.error}
                        </p>
                      )}
                    </div>
                    <time
                      dateTime={event.created_at}
                      title={fullTime(event.created_at)}
                      className="shrink-0 whitespace-nowrap pt-0.5 text-[11px] text-slate-500"
                    >
                      {timeAgo(event.created_at)}
                    </time>
                  </motion.li>
                );
              })}
            </ul>
          )}
        </section>

        {/* Setup / connect panel */}
        <section className="mt-4 rounded-xl bg-slate-900/70 ring-1 ring-slate-800">
          <button
            onClick={() => setSetupOpen((v) => !v)}
            aria-expanded={setupOpen}
            className="flex w-full items-center justify-between px-4 py-3 text-left text-sm font-medium text-slate-200"
          >
            <span className="flex items-center gap-2">
              <Radio className="h-4 w-4 text-emerald-400" aria-hidden="true" />
              Bot ko is dashboard se connect karo
            </span>
            <ChevronDown
              className={`h-4 w-4 text-slate-400 transition-transform ${setupOpen ? "rotate-180" : ""}`}
              aria-hidden="true"
            />
          </button>
          {setupOpen && (
            <div className="space-y-4 border-t border-slate-800 px-4 py-4 text-sm text-slate-300">
              <p>
                Bot (v2.3+) apne file uploads aur network errors khud is
                website par bhej sakta hai. Bot ke{" "}
                <code className="rounded bg-slate-800 px-1.5 py-0.5 font-mono text-xs">
                  .env
                </code>{" "}
                me ye lines add karo:
              </p>
              <div className="rounded-lg bg-slate-950 ring-1 ring-slate-800 p-3">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] uppercase tracking-wide text-slate-500">
                    .env
                  </span>
                  <button
                    onClick={() =>
                      copyText(
                        "env",
                        `WEB_DASHBOARD_ENABLED=true\nWEB_DASHBOARD_WEBHOOK_URL=${origin}/api/events\n# Optional shared secret:\n# DASHBOARD_INGEST_TOKEN aur WEB_DASHBOARD_WEBHOOK_TOKEN same rakho`
                      )
                    }
                    className="inline-flex items-center gap-1 rounded px-2 py-1 text-[11px] text-slate-400 hover:bg-slate-800 hover:text-slate-200"
                    aria-label="Copy .env lines"
                  >
                    {copied === "env" ? (
                      <Check className="h-3.5 w-3.5 text-emerald-400" />
                    ) : (
                      <Copy className="h-3.5 w-3.5" />
                    )}
                    {copied === "env" ? "Copied" : "Copy"}
                  </button>
                </div>
                <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-all font-mono text-xs text-emerald-300">
{`WEB_DASHBOARD_ENABLED=true
WEB_DASHBOARD_WEBHOOK_URL=${origin || "https://<this-site>/api/events"}`}
                </pre>
              </div>
              <ul className="list-disc space-y-1 pl-5 text-xs text-slate-400">
                <li>
                  Bot ka apna built-in dashboard bhi hai — wahi page bot ke
                  server par{" "}
                  <code className="rounded bg-slate-800 px-1 py-0.5 font-mono">
                    http://&lt;host&gt;:8080
                  </code>{" "}
                  par milta hai (direct poll, internet zaroori nahi).
                </li>
                <li>
                  Telegram credentials (API_ID, API_HASH, BOT_TOKEN) sirf bot
                  ke apne .env me rehte hain — website ko kabhi nahi milte.
                </li>
                <li>
                  Baaki bot ka behaviour bilkul same rehta hai — /set, /batch,
                  /bot, story/post mode, sab pehle jaisa hi.
                </li>
              </ul>
            </div>
          )}
        </section>
      </main>

      <footer className="mt-auto border-t border-slate-800/70 bg-slate-950">
        <div className="mx-auto flex w-full max-w-5xl flex-wrap items-center justify-between gap-2 px-4 py-4 text-xs text-slate-500 sm:px-6">
          <span>TeraFast v2.3 · Activity Dashboard</span>
          <span>
            Polls /api/logs every {POLL_INTERVAL_MS / 1000}s · file uploads &
            network errors
          </span>
        </div>
      </footer>
    </div>
  );
}
