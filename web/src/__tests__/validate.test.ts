import { describe, expect, it } from "vitest";
import { mergeJobs, parseJob, parseJobList } from "../validate";
import type { Job } from "../types";

const valid = {
  job_id: "stripe:1",
  company: "Stripe",
  title: "Software Engineer Intern",
  url: "https://stripe.com/jobs/1",
  source: "greenhouse",
  location: "San Francisco",
  date_posted: "2026-09-18T00:00:00Z",
  first_seen: "2026-09-26T12:00:00Z",
  last_seen: "2026-09-26T12:00:00Z",
  relevance_score: 73,
  priority: "high",
  industry: "Payments",
  employment_type: "Intern",
  description: "Build payments.",
  notification_sent: false,
};

describe("parseJob", () => {
  it("accepts a well-formed record", () => {
    const job = parseJob(valid);
    expect(job).not.toBeNull();
    expect(job?.job_id).toBe("stripe:1");
    expect(job?.relevance_score).toBe(73);
  });

  it("fills sensible defaults for missing optional fields", () => {
    const job = parseJob({
      job_id: "a:1",
      company: "A",
      title: "Intern",
      url: "https://a.example.com/1",
    });
    expect(job?.location).toBeNull();
    expect(job?.source).toBe("unknown");
    expect(job?.relevance_score).toBe(0);
    expect(job?.notification_sent).toBe(false);
  });

  it.each(["job_id", "company", "title"])("rejects a record missing %s", (field) => {
    const broken: Record<string, unknown> = { ...valid };
    delete broken[field];
    expect(parseJob(broken)).toBeNull();
  });

  it.each(["", "   "])("rejects a blank required field (%j)", (blank) => {
    expect(parseJob({ ...valid, title: blank })).toBeNull();
  });

  it("rejects a non-http url", () => {
    // A javascript: url must never become a clickable Apply button.
    expect(parseJob({ ...valid, url: "javascript:alert(1)" })).toBeNull();
    expect(parseJob({ ...valid, url: "/relative" })).toBeNull();
    expect(parseJob({ ...valid, url: "" })).toBeNull();
  });

  it("clamps an out-of-range score instead of trusting it", () => {
    expect(parseJob({ ...valid, relevance_score: 5000 })?.relevance_score).toBe(100);
    expect(parseJob({ ...valid, relevance_score: -20 })?.relevance_score).toBe(0);
    expect(parseJob({ ...valid, relevance_score: Number.NaN })?.relevance_score).toBe(0);
    expect(parseJob({ ...valid, relevance_score: "73" })?.relevance_score).toBe(0);
  });

  it.each([null, undefined, 42, "string", []])("rejects %j", (input) => {
    expect(parseJob(input)).toBeNull();
  });
});

describe("parseJobList", () => {
  it("parses a normal response", () => {
    const result = parseJobList({ count: 1, limit: 50, jobs: [valid] });
    expect(result.jobs).toHaveLength(1);
    expect(result.limit).toBe(50);
  });

  it("drops unusable records but keeps the good ones", () => {
    // A single bad row must not blank the whole feed.
    const result = parseJobList({
      jobs: [valid, { job_id: "broken" }, { ...valid, job_id: "stripe:2" }],
    });
    expect(result.jobs.map((job) => job.job_id)).toEqual(["stripe:1", "stripe:2"]);
    expect(result.count).toBe(2);
  });

  it("de-duplicates by job id", () => {
    const result = parseJobList({ jobs: [valid, valid, { ...valid, title: "Changed" }] });
    expect(result.jobs).toHaveLength(1);
    expect(result.jobs[0]?.title).toBe("Software Engineer Intern");
  });

  it.each([{}, { jobs: null }, { jobs: "nope" }, null, undefined])(
    "returns an empty list for %j",
    (input) => {
      expect(parseJobList(input).jobs).toEqual([]);
    },
  );
});

describe("mergeJobs", () => {
  const job = (id: string, firstSeen: string): Job => parseJob({ ...valid, job_id: id, first_seen: firstSeen })!;

  it("adds new jobs", () => {
    const merged = mergeJobs([job("a:1", "2026-09-26T12:00:00Z")], [job("a:2", "2026-09-26T13:00:00Z")]);
    expect(merged.map((j) => j.job_id)).toEqual(["a:2", "a:1"]);
  });

  it("never duplicates an existing job", () => {
    // The property that stops a push + refresh showing one job twice.
    const existing = [job("a:1", "2026-09-26T12:00:00Z")];
    const merged = mergeJobs(existing, [job("a:1", "2026-09-26T12:00:00Z")]);
    expect(merged).toHaveLength(1);
  });

  it("lets an incoming job update an existing one", () => {
    const updated = { ...job("a:1", "2026-09-26T12:00:00Z"), title: "Backend Intern" };
    const merged = mergeJobs([job("a:1", "2026-09-26T12:00:00Z")], [updated]);
    expect(merged[0]?.title).toBe("Backend Intern");
  });

  it("orders newest first", () => {
    const merged = mergeJobs(
      [job("a:1", "2026-09-26T10:00:00Z"), job("a:3", "2026-09-26T14:00:00Z")],
      [job("a:2", "2026-09-26T12:00:00Z")],
    );
    expect(merged.map((j) => j.job_id)).toEqual(["a:3", "a:2", "a:1"]);
  });

  it("is stable when timestamps tie", () => {
    const merged = mergeJobs([], [job("b:1", "2026-09-26T12:00:00Z"), job("a:1", "2026-09-26T12:00:00Z")]);
    expect(merged.map((j) => j.job_id)).toEqual(["a:1", "b:1"]);
  });
});
