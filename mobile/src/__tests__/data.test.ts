/**
 * Validation, merging, formatting and the API client - the client's data layer.
 *
 * The backend is treated as untrusted (PRD §14 L5 "malformed backend data fails
 * gracefully"), so a bad record is dropped rather than allowed to blank the feed,
 * and a non-http apply URL is refused outright so nothing tappable can point at a
 * `javascript:` scheme.
 */
import { ApiError, fetchJob, fetchRecentJobs, registerDevice } from "../api";
import { formatExact, formatWhen } from "../format";
import { mergeJobs, parseJob, parseJobList } from "../validate";

const good = {
  job_id: "stripe:1",
  company: "Stripe",
  title: "Software Engineer Intern",
  url: "https://stripe.com/jobs/1",
  location: "SF",
  source: "greenhouse",
  first_seen: "2026-09-26T12:00:00+00:00",
  relevance_score: 80,
};

describe("parseJob", () => {
  it("accepts a complete record", () => {
    expect(parseJob(good)).toMatchObject({ job_id: "stripe:1", relevance_score: 80 });
  });

  it.each([
    ["no job_id", { ...good, job_id: "" }],
    ["no company", { ...good, company: "   " }],
    ["no title", { ...good, title: undefined }],
    ["no url", { ...good, url: "" }],
    ["a javascript: url", { ...good, url: "javascript:alert(1)" }],
    ["a custom-scheme url", { ...good, url: "internshipmonitor://jobs/1" }],
    ["a protocol-relative url", { ...good, url: "//stripe.com/jobs/1" }],
    ["not an object", "stripe:1"],
    ["null", null],
  ])("rejects %s", (_label, raw) => {
    expect(parseJob(raw)).toBeNull();
  });

  it("supplies defaults for the optional fields", () => {
    const job = parseJob({ job_id: "a", company: "A", title: "T", url: "https://a/1" });
    expect(job).toMatchObject({
      location: null,
      source: "unknown",
      priority: "medium",
      relevance_score: 0,
      notification_sent: false,
    });
  });

  it("clamps a nonsensical relevance score", () => {
    expect(parseJob({ ...good, relevance_score: 5000 })?.relevance_score).toBe(100);
    expect(parseJob({ ...good, relevance_score: -3 })?.relevance_score).toBe(0);
    expect(parseJob({ ...good, relevance_score: Number.NaN })?.relevance_score).toBe(0);
  });
});

describe("parseJobList", () => {
  it("keeps the usable records and drops the rest", () => {
    const list = parseJobList({ jobs: [good, { junk: true }, null, 7] });
    expect(list.jobs).toHaveLength(1);
    expect(list.count).toBe(1);
  });

  it("de-duplicates by job id", () => {
    expect(parseJobList({ jobs: [good, { ...good, title: "Other" }] }).jobs).toHaveLength(1);
  });

  it.each([null, undefined, {}, { jobs: "nope" }])("survives %p", (raw) => {
    expect(parseJobList(raw).jobs).toEqual([]);
  });
});

describe("mergeJobs", () => {
  const a = parseJob({ ...good, job_id: "a", first_seen: "2026-09-26T10:00:00+00:00" })!;
  const b = parseJob({ ...good, job_id: "b", first_seen: "2026-09-26T12:00:00+00:00" })!;

  it("never duplicates a job that is already on screen", () => {
    expect(mergeJobs([a, b], [a]).map((job) => job.job_id)).toEqual(["b", "a"]);
  });

  it("puts the newest first", () => {
    expect(mergeJobs([a], [b]).map((job) => job.job_id)).toEqual(["b", "a"]);
  });

  it("prefers the incoming version of a job", () => {
    const updated = { ...a, title: "Updated" };
    expect(mergeJobs([a], [updated])[0]?.title).toBe("Updated");
  });

  it("breaks first_seen ties deterministically", () => {
    const x = { ...a, job_id: "x" };
    const y = { ...a, job_id: "y" };
    expect(mergeJobs([y], [x]).map((job) => job.job_id)).toEqual(["x", "y"]);
  });
});

describe("formatting", () => {
  const now = new Date("2026-09-26T12:00:00Z");

  it.each([
    ["2026-09-26T11:59:40Z", "just now"],
    ["2026-09-26T11:45:00Z", "15m ago"],
    ["2026-09-26T09:00:00Z", "3h ago"],
    ["2026-09-24T12:00:00Z", "2d ago"],
    ["2026-07-01T12:00:00Z", "2026-07-01"],
  ])("renders %s as %s", (iso, expected) => {
    expect(formatWhen(iso, now)).toBe(expected);
  });

  it.each([null, undefined, "", "not a date"])("renders %p as empty", (iso) => {
    expect(formatWhen(iso, now)).toBe("");
  });

  it("renders an absolute timestamp for the detail screen", () => {
    expect(formatExact("2026-09-26T12:00:00+00:00")).toBe("2026-09-26 12:00 UTC");
  });

  it.each([null, undefined, "garbage"])("renders %p as Unknown", (iso) => {
    expect(formatExact(iso)).toBe("Unknown");
  });
});

describe("the API client", () => {
  const response = (body: unknown, status = 200) =>
    ({ ok: status >= 200 && status < 300, status, json: async () => body }) as Response;

  afterEach(() => {
    delete (global as { fetch?: unknown }).fetch;
  });

  it("asks for recent jobs with a limit", async () => {
    const fetchMock = jest.fn(async (_input: unknown) => response({ jobs: [good] }));
    global.fetch = fetchMock as unknown as typeof fetch;
    const list = await fetchRecentJobs({ limit: 10 });
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("/jobs?limit=10");
    expect(list.jobs).toHaveLength(1);
  });

  it("url-encodes a job id containing a colon", async () => {
    const fetchMock = jest.fn(async (_input: unknown) => response({ job: good }));
    global.fetch = fetchMock as unknown as typeof fetch;
    await fetchJob("stripe:1");
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("/jobs/stripe%3A1");
  });

  it("reports a 404 as a 404", async () => {
    global.fetch = (async () => response({}, 404)) as unknown as typeof fetch;
    await expect(fetchJob("gone")).rejects.toMatchObject({ status: 404 });
  });

  it("explains a server error", async () => {
    global.fetch = (async () => response({}, 503)) as unknown as typeof fetch;
    await expect(fetchRecentJobs()).rejects.toThrow(/returned an error \(503\)/);
  });

  it("explains an unreachable service", async () => {
    global.fetch = (async () => {
      throw new TypeError("Network request failed");
    }) as unknown as typeof fetch;
    await expect(fetchRecentJobs()).rejects.toThrow(/Check your connection/);
  });

  it("distinguishes a cancelled request", async () => {
    global.fetch = (async () => {
      const error = new Error("aborted");
      error.name = "AbortError";
      throw error;
    }) as unknown as typeof fetch;
    await expect(fetchRecentJobs()).rejects.toThrow("Request cancelled");
  });

  it("explains a body it cannot read", async () => {
    global.fetch = (async () =>
      ({
        ok: true,
        status: 200,
        json: async () => {
          throw new Error("not json");
        },
      }) as unknown as Response) as unknown as typeof fetch;
    await expect(fetchRecentJobs()).rejects.toThrow(/could not read/);
  });

  it("rejects a detail response whose job is unusable", async () => {
    global.fetch = (async () => response({ job: { junk: true } })) as unknown as typeof fetch;
    await expect(fetchJob("a")).rejects.toBeInstanceOf(ApiError);
  });

  it("refuses to register a device with no write token configured", async () => {
    // app.json ships no apiWriteToken, so this is the default build's behaviour:
    // fail closed and say why, rather than POST unauthenticated.
    global.fetch = jest.fn() as unknown as typeof fetch;
    await expect(registerDevice("ios-x", "ExponentPushToken[x]")).rejects.toThrow(/write token/);
    expect(global.fetch).not.toHaveBeenCalled();
  });
});
