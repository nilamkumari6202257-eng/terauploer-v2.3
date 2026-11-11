import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/db";
import {
  ActivityEvent,
  EventType,
  LogsResponse,
  isEventType,
} from "@/lib/events";

export const dynamic = "force-dynamic";

function toEvent(row: {
  id: number;
  type: string;
  message: string;
  fileName: string | null;
  fileSize: bigint | null;
  link: string | null;
  source: string | null;
  target: string | null;
  durationSec: number | null;
  error: string | null;
  isSample: boolean;
  createdAt: Date;
}): ActivityEvent {
  return {
    id: row.id,
    type: row.type as EventType,
    message: row.message,
    file_name: row.fileName,
    // BigInt (SQLite) -> Number is safe below 2^53 bytes (~9 PB).
    file_size: row.fileSize === null ? null : Number(row.fileSize),
    link: row.link,
    source: row.source,
    target: row.target,
    duration_sec: row.durationSec,
    error: row.error,
    sample: row.isSample,
    created_at: row.createdAt.toISOString(),
  };
}

/**
 * GET /api/logs — latest activity events, polled by the dashboard.
 * Query params: type (upload|upload_failed|network_error|info),
 *               limit (1..200, default 100), after (event id, exclusive).
 */
export async function GET(request: NextRequest) {
  try {
    const params = request.nextUrl.searchParams;
    const typeParam = params.get("type");
    const type = typeParam && isEventType(typeParam) ? typeParam : undefined;
    const limit = Math.min(
      200,
      Math.max(1, Number(params.get("limit") ?? 100) || 100)
    );
    const after = Number(params.get("after") ?? 0) || 0;

    const rows = await db.activityEvent.findMany({
      where: {
        ...(type ? { type } : {}),
        ...(after > 0 ? { id: { gt: after } } : {}),
      },
      orderBy: { id: "desc" },
      take: limit,
    });

    const grouped = await db.activityEvent.groupBy({
      by: ["type"],
      _count: { _all: true },
    });
    const counts: Record<string, number> = {};
    let total = 0;
    for (const g of grouped) {
      counts[g.type] = g._count._all;
      total += g._count._all;
    }

    const last = await db.activityEvent.findFirst({
      orderBy: { id: "desc" },
      select: { createdAt: true },
    });

    const body: LogsResponse = {
      ok: true,
      events: rows.map(toEvent),
      stats: {
        uploads: counts["upload"] ?? 0,
        upload_failed: counts["upload_failed"] ?? 0,
        network_errors: counts["network_error"] ?? 0,
        infos: counts["info"] ?? 0,
        total,
        last_event_at: last ? last.createdAt.toISOString() : null,
      },
      server_time: new Date().toISOString(),
    };
    return NextResponse.json(body);
  } catch (error) {
    console.error("GET /api/logs failed:", error);
    return NextResponse.json(
      { ok: false, error: "Failed to load events" },
      { status: 500 }
    );
  }
}
