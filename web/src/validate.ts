import type { Job, JobListResponse } from "./types";

/**
 * Coerce whatever the backend actually sent into a `Job`, or reject it.
 *
 * The client must survive a backend that is older, newer, or briefly broken:
 * PRD §14 Level 5 requires malformed data to fail gracefully. So each record is
 * validated individually and bad ones are dropped, rather than one bad row
 * blanking the whole feed.
 */
export function parseJob(raw: unknown): Job | null {
  if (raw === null || typeof raw !== "object") return null;
  const value = raw as Record<string, unknown>;

  const jobId = typeof value.job_id === "string" ? value.job_id.trim() : "";
  const company = typeof value.company === "string" ? value.company.trim() : "";
  const title = typeof value.title === "string" ? value.title.trim() : "";
  const url = typeof value.url === "string" ? value.url.trim() : "";

  // These four are what every screen needs; without them there is nothing to show.
  if (!jobId || !company || !title) return null;
  // Only http(s) links are rendered: a `javascript:` URL from a compromised
  // upstream board must never become a clickable button.
  if (!/^https?:\/\//i.test(url)) return null;

  const str = (key: string): string | null =>
    typeof value[key] === "string" && (value[key] as string).length > 0
      ? (value[key] as string)
      : null;

  const score = typeof value.relevance_score === "number" ? value.relevance_score : 0;

  return {
    job_id: jobId,
    company,
    title,
    url,
    location: str("location"),
    source: str("source") ?? "unknown",
    date_posted: str("date_posted"),
    first_seen: str("first_seen"),
    last_seen: str("last_seen"),
    relevance_score: Number.isFinite(score) ? Math.max(0, Math.min(100, score)) : 0,
    priority: str("priority") ?? "medium",
    industry: str("industry") ?? "unknown",
    employment_type: str("employment_type"),
    description: str("description"),
    notification_sent: value.notification_sent === true,
  };
}

/** Parse a list response, dropping unusable records and de-duplicating by id. */
export function parseJobList(raw: unknown): JobListResponse {
  const value = (raw ?? {}) as Record<string, unknown>;
  const rawJobs = Array.isArray(value.jobs) ? value.jobs : [];
  const seen = new Set<string>();
  const jobs: Job[] = [];
  for (const entry of rawJobs) {
    const job = parseJob(entry);
    // De-duplicating here means a repeated push event or a double-appended
    // response can never show the same job twice (PRD §14 Level 5).
    if (job && !seen.has(job.job_id)) {
      seen.add(job.job_id);
      jobs.push(job);
    }
  }
  return {
    count: jobs.length,
    limit: typeof value.limit === "number" ? value.limit : jobs.length,
    jobs,
  };
}

/** Merge newly arrived jobs into a list without creating duplicates. */
export function mergeJobs(existing: Job[], incoming: Job[]): Job[] {
  const byId = new Map(existing.map((job) => [job.job_id, job]));
  for (const job of incoming) byId.set(job.job_id, job);
  return [...byId.values()].sort((a, b) => {
    const left = a.first_seen ?? "";
    const right = b.first_seen ?? "";
    if (left === right) return a.job_id.localeCompare(b.job_id);
    return left < right ? 1 : -1;
  });
}
