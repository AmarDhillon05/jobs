import type { NotificationPayload } from "./types";

export type Route = { name: "feed" } | { name: "job"; jobId: string } | { name: "unknown"; path: string };

/**
 * Parse a path into a route.
 *
 * Job ids contain a colon (`stripe:1`), so deep links percent-encode them. The
 * decode here is the other half of the backend's `JobRecord.deep_link`, and the
 * pair is what makes "tap a notification, land on the right job" work.
 */
export function parseRoute(pathname: string): Route {
  const path = pathname.replace(/\/+$/, "") || "/";
  if (path === "/" || path === "/jobs") return { name: "feed" };

  const match = /^\/jobs\/(.+)$/.exec(path);
  if (match?.[1]) {
    let jobId = match[1];
    try {
      jobId = decodeURIComponent(jobId);
    } catch {
      // A malformed escape sequence: use the raw segment rather than throwing.
    }
    if (jobId) return { name: "job", jobId };
  }
  return { name: "unknown", path };
}

export function jobPath(jobId: string): string {
  return `/jobs/${encodeURIComponent(jobId)}`;
}

/**
 * Work out where a notification should take the user.
 *
 * Prefers the explicit `job_id`, then the path inside `deep_link` (which may be
 * absolute and from a different origin), then the first id in `job_ids`. Returns
 * the feed when the payload names no single job, which is the correct
 * destination for a grouped alert.
 */
export function routeForNotification(payload: NotificationPayload | null | undefined): Route {
  if (!payload) return { name: "feed" };

  if (typeof payload.job_id === "string" && payload.job_id.trim()) {
    return { name: "job", jobId: payload.job_id.trim() };
  }

  if (typeof payload.deep_link === "string" && payload.deep_link.trim()) {
    const link = payload.deep_link.trim();
    try {
      // Absolute or relative: resolving against a base handles both.
      const url = new URL(link, "http://placeholder.invalid");
      const route = parseRoute(url.pathname);
      if (route.name !== "unknown") return route;
    } catch {
      // Fall through to job_ids.
    }
  }

  if (typeof payload.job_ids === "string" && payload.job_ids.trim()) {
    const ids = payload.job_ids.split(",").map((id) => id.trim()).filter(Boolean);
    if (ids.length === 1 && ids[0]) return { name: "job", jobId: ids[0] };
  }

  return { name: "feed" };
}

/** Parse the `data` map of an incoming push, tolerating anything. */
export function parseNotificationPayload(raw: unknown): NotificationPayload | null {
  if (raw === null || typeof raw !== "object") return null;
  const source = raw as Record<string, unknown>;
  const nested = source.data;
  const value = (nested !== null && typeof nested === "object" ? nested : source) as Record<
    string,
    unknown
  >;
  const pick = (key: string): string | undefined =>
    typeof value[key] === "string" ? (value[key] as string) : undefined;

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
    const found = pick(key);
    if (found !== undefined) payload[key] = found;
  }
  return Object.keys(payload).length > 0 ? payload : null;
}
