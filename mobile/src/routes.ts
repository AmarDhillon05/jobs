import type { NotificationPayload } from "./types";

export type Route = { name: "feed" } | { name: "job"; jobId: string };

/**
 * Work out where a notification should take the user.
 *
 * Prefers the explicit `job_id`, then the `/jobs/<id>` path inside `deep_link`
 * (which is an absolute https URL to the web client, or an `internshipmonitor://`
 * deep link), then a single id in `job_ids`. Falls back to the feed, which is the
 * right destination for a grouped alert naming several jobs.
 *
 * Job ids contain a colon (`stripe:1`), so the backend percent-encodes them in
 * deep links and this decodes them. That pair is what makes "tap the notification,
 * land on the right job" work.
 */
export function routeForNotification(payload: NotificationPayload | null | undefined): Route {
  if (!payload) return { name: "feed" };

  if (typeof payload.job_id === "string" && payload.job_id.trim()) {
    return { name: "job", jobId: payload.job_id.trim() };
  }

  if (typeof payload.deep_link === "string" && payload.deep_link.trim()) {
    const jobId = jobIdFromPath(payload.deep_link.trim());
    if (jobId) return { name: "job", jobId };
  }

  if (typeof payload.job_ids === "string" && payload.job_ids.trim()) {
    const ids = payload.job_ids.split(",").map((id) => id.trim()).filter(Boolean);
    if (ids.length === 1 && ids[0]) return { name: "job", jobId: ids[0] };
  }

  return { name: "feed" };
}

/** Pull a job id out of any `.../jobs/<url-encoded id>` link. */
export function jobIdFromPath(link: string): string | null {
  const match = /\/jobs\/([^/?#]+)/.exec(link);
  if (!match?.[1]) return null;
  try {
    return decodeURIComponent(match[1]) || null;
  } catch {
    // A malformed escape sequence: use the raw segment rather than throwing.
    return match[1];
  }
}

/** Read the `data` map off an incoming notification, tolerating any shape. */
export function parseNotificationPayload(raw: unknown): NotificationPayload | null {
  if (raw === null || typeof raw !== "object") return null;
  const source = raw as Record<string, unknown>;
  // Expo nests the payload under request.content.data; accept that, a bare
  // `data` wrapper, or a flat map.
  const nested =
    (source as { request?: { content?: { data?: unknown } } }).request?.content?.data ??
    source.data ??
    source;
  const value = (nested !== null && typeof nested === "object" ? nested : {}) as Record<
    string,
    unknown
  >;

  const payload: NotificationPayload = {};
  for (const key of [
    "schema_version",
    "urgency",
    "deep_link",
    "job_id",
    "job_ids",
    "job_count",
    "apply_url",
  ] as const) {
    if (typeof value[key] === "string") payload[key] = value[key] as string;
  }
  return Object.keys(payload).length > 0 ? payload : null;
}
