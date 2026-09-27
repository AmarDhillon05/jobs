import type { Job, JobListResponse } from "./types";
import { parseJob, parseJobList } from "./validate";

/**
 * The API base URL. Injected at build time so the same bundle can point at a
 * local server or a deployed API Gateway. No credential is ever shipped here -
 * read endpoints are public and writes use a token the user supplies.
 */
export const API_BASE: string =
  (import.meta.env?.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ??
  "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function getJson(path: string, signal?: AbortSignal): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      signal,
      headers: { Accept: "application/json" },
    });
  } catch (cause) {
    // Offline, DNS failure, CORS rejection: all indistinguishable here, and all
    // need the same "couldn't reach the server" message.
    throw new ApiError(
      cause instanceof Error && cause.name === "AbortError"
        ? "Request cancelled"
        : "Could not reach the jobs service. Check your connection and try again.",
    );
  }

  if (response.status === 404) throw new ApiError("Not found", 404);
  if (!response.ok) {
    throw new ApiError(`The jobs service returned an error (${response.status}).`, response.status);
  }

  try {
    return await response.json();
  } catch {
    throw new ApiError("The jobs service returned a response we could not read.");
  }
}

export async function fetchRecentJobs(
  options: { limit?: number; signal?: AbortSignal } = {},
): Promise<JobListResponse> {
  const limit = options.limit ?? 50;
  return parseJobList(await getJson(`/jobs?limit=${limit}`, options.signal));
}

export async function fetchJob(jobId: string, signal?: AbortSignal): Promise<Job> {
  const raw = await getJson(`/jobs/${encodeURIComponent(jobId)}`, signal);
  const job = parseJob((raw as { job?: unknown } | null)?.job);
  if (!job) throw new ApiError("That job could not be read.");
  return job;
}

export async function registerDevice(
  deviceId: string,
  token: string,
  writeToken: string,
): Promise<void> {
  const response = await fetch(`${API_BASE}/devices/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Api-Token": writeToken },
    body: JSON.stringify({ device_id: deviceId, token, transport: "webpush" }),
  });
  if (!response.ok) {
    throw new ApiError(`Could not register for notifications (${response.status}).`, response.status);
  }
}
