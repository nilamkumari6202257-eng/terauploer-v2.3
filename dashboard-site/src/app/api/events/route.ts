import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/db";
import { IngestEvent, sanitizeIngestEvent } from "@/lib/events";

export const dynamic = "force-dynamic";

/**
 * POST /api/events — webhook ingest endpoint.
 *
 * The bot (services/activity_log.py forwarder) POSTs batches of activity
 * events here so they show up live on the deployed dashboard.
 *
 * Auth: if the DASHBOARD_INGEST_TOKEN env var is set, the request must send
 * the same value in the "x-dashboard-token" header (or "token" body field).
 * If the env var is unset the endpoint is open (demo mode).
 *
 * Body: { events: IngestEvent[] }  or a single IngestEvent object.
 */
export async function POST(request: NextRequest) {
  try {
    const expectedToken = process.env.DASHBOARD_INGEST_TOKEN?.trim();
    if (expectedToken) {
      const headerToken = request.headers.get("x-dashboard-token")?.trim();
      let bodyToken: string | undefined;
      try {
        const raw = await request.json();
        bodyToken =
          typeof raw?.token === "string" ? raw.token.trim() : undefined;
      } catch {
        /* body parse errors handled below */
      }
      if (headerToken !== expectedToken && bodyToken !== expectedToken) {
        return NextResponse.json(
          { ok: false, error: "Invalid dashboard token" },
          { status: 401 }
        );
      }
    }

    let payload: unknown;
    try {
      payload = await request.json();
    } catch {
      return NextResponse.json(
        { ok: false, error: "Invalid JSON body" },
        { status: 400 }
      );
    }

    const rawEvents =
      payload && typeof payload === "object" && Array.isArray((payload as any).events)
        ? (payload as any).events
        : Array.isArray(payload)
          ? payload
          : [payload];

    const clean: IngestEvent[] = [];
    for (const raw of rawEvents) {
      const event = sanitizeIngestEvent(raw);
      if (event) clean.push(event);
    }
    if (clean.length === 0) {
      return NextResponse.json(
        { ok: false, error: "No valid events in payload" },
        { status: 400 }
      );
    }
    if (clean.length > 200) clean.length = 200;

    const result = await db.activityEvent.createMany({
      data: clean.map((e) => ({
        type: e.type,
        message: e.message,
        fileName: e.file_name,
        fileSize:
          e.file_size === null || e.file_size === undefined
            ? null
            : BigInt(Math.round(e.file_size)),
        link: e.link,
        source: e.source ?? "webhook",
        target: e.target,
        durationSec: e.duration_sec,
        error: e.error,
        isSample: false,
        createdAt: e.created_at ? new Date(e.created_at) : new Date(),
      })),
    });

    return NextResponse.json({ ok: true, ingested: result.count });
  } catch (error) {
    console.error("POST /api/events failed:", error);
    return NextResponse.json(
      { ok: false, error: "Failed to ingest events" },
      { status: 500 }
    );
  }
}

/**
 * DELETE /api/events — clear stored events (dashboard "Clear log" button).
 * Sample events can be cleared always; real events only with the token
 * (or when no token is configured).
 */
export async function DELETE(request: NextRequest) {
  try {
    const onlySamples = request.nextUrl.searchParams.get("samples") === "1";
    const expectedToken = process.env.DASHBOARD_INGEST_TOKEN?.trim();
    const headerToken = request.headers.get("x-dashboard-token")?.trim();

    if (!onlySamples) {
      if (expectedToken && headerToken !== expectedToken) {
        return NextResponse.json(
          { ok: false, error: "Invalid dashboard token" },
          { status: 401 }
        );
      }
      await db.activityEvent.deleteMany({});
      return NextResponse.json({ ok: true, cleared: "all" });
    }

    await db.activityEvent.deleteMany({ where: { isSample: true } });
    return NextResponse.json({ ok: true, cleared: "samples" });
  } catch (error) {
    console.error("DELETE /api/events failed:", error);
    return NextResponse.json(
      { ok: false, error: "Failed to clear events" },
      { status: 500 }
    );
  }
}
