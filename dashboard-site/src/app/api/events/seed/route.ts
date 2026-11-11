import { NextResponse } from "next/server";
import { db } from "@/lib/db";

export const dynamic = "force-dynamic";

/**
 * POST /api/events/seed — insert a few clearly-marked SAMPLE events so the
 * log display can be demoed before the bot is connected.
 * Sample events are flagged (isSample=true, source="sample") and can be
 * removed with the dashboard's "Remove sample data" action.
 */
export async function POST() {
  try {
    const now = Date.now();
    const minute = 60_000;

    const samples = [
      {
        type: "info",
        message: "Bot started — dashboard connected",
        source: "sample",
        target: null as string | null,
        fileName: null as string | null,
        fileSize: null as number | null,
        link: null as string | null,
        durationSec: null as number | null,
        error: null as string | null,
        createdAt: new Date(now - 9 * minute),
      },
      {
        type: "upload",
        message: "Uploaded sample_video_4k.mp4 to TeraBox",
        source: "sample",
        target: "Sample Channel",
        fileName: "sample_video_4k.mp4",
        fileSize: 734_003_200n,
        link: "https://1024terabox.com/s/sample-video-link",
        durationSec: 118.4,
        error: null,
        createdAt: new Date(now - 8 * minute),
      },
      {
        type: "network_error",
        message: "TeraBox part upload timed out — retrying (attempt 1/8)",
        source: "sample",
        target: "Sample Channel",
        fileName: "sample_movie_1080p.mkv",
        fileSize: 2_147_483_648n,
        link: null,
        durationSec: null,
        error: "ReadTimeout: HTTPSConnectionPool(host='dm.1024terabox.com', port=443): Read timed out.",
        createdAt: new Date(now - 6 * minute),
      },
      {
        type: "upload",
        message: "Uploaded sample_movie_1080p.mkv to TeraBox (after 2 retries)",
        source: "sample",
        target: "Sample Channel",
        fileName: "sample_movie_1080p.mkv",
        fileSize: 2_147_483_648n,
        link: "https://1024terabox.com/s/sample-movie-link",
        durationSec: 402.7,
        error: null,
        createdAt: new Date(now - 5 * minute),
      },
      {
        type: "network_error",
        message: "Telegram FloodWait 34s — batch paused briefly",
        source: "sample",
        target: "Sample Channel",
        fileName: null,
        fileSize: null,
        link: null,
        durationSec: null,
        error: "FloodWait: 34 seconds",
        createdAt: new Date(now - 3 * minute),
      },
      {
        type: "upload_failed",
        message: "Upload failed: TeraBox cookie expired",
        source: "sample",
        target: "Sample Channel",
        fileName: "sample_clip.mp3",
        fileSize: 8_388_608n,
        link: null,
        durationSec: 3.1,
        error: "TeraBox error: errno=-6 not login (cookie expired, /bind se fresh cookie do)",
        createdAt: new Date(now - 2 * minute),
      },
      {
        type: "upload",
        message: "Uploaded sample_story.mp4 to TeraBox",
        source: "sample",
        target: "@sample_user",
        fileName: "sample_story.mp4",
        fileSize: 24_117_248n,
        link: "https://1024terabox.com/s/sample-story-link",
        durationSec: 6.9,
        error: null,
        createdAt: new Date(now - 1 * minute),
      },
    ];

    const result = await db.activityEvent.createMany({
      data: samples.map((s) => ({ ...s, isSample: true })),
    });

    return NextResponse.json({ ok: true, seeded: result.count });
  } catch (error) {
    console.error("POST /api/events/seed failed:", error);
    return NextResponse.json(
      { ok: false, error: "Failed to seed sample events" },
      { status: 500 }
    );
  }
}
