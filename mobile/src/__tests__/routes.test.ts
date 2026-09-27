import { describe, expect, it } from "vitest";
import {
  jobPath,
  parseNotificationPayload,
  parseRoute,
  routeForNotification,
} from "../routes";

describe("parseRoute", () => {
  it("maps the root and /jobs to the feed", () => {
    expect(parseRoute("/")).toEqual({ name: "feed" });
    expect(parseRoute("/jobs")).toEqual({ name: "feed" });
    expect(parseRoute("/jobs/")).toEqual({ name: "feed" });
  });

  it("extracts a job id", () => {
    expect(parseRoute("/jobs/testco:42")).toEqual({ name: "job", jobId: "testco:42" });
  });

  it("decodes a percent-encoded id, which is how deep links arrive", () => {
    // The backend encodes the colon, so this is the real-world shape.
    expect(parseRoute("/jobs/stripe%3A1")).toEqual({ name: "job", jobId: "stripe:1" });
  });

  it("decodes hash-style fallback ids", () => {
    const id = "jane-street:h:0123456789abcdef0123456789abcdef";
    expect(parseRoute(`/jobs/${encodeURIComponent(id)}`)).toEqual({ name: "job", jobId: id });
  });

  it("tolerates a malformed escape sequence instead of throwing", () => {
    expect(parseRoute("/jobs/%E0%A4%A")).toEqual({ name: "job", jobId: "%E0%A4%A" });
  });

  it("reports unknown paths", () => {
    expect(parseRoute("/settings")).toEqual({ name: "unknown", path: "/settings" });
  });

  it("round-trips with jobPath", () => {
    const id = "d. e. shaw:req 1/2";
    expect(parseRoute(jobPath(id))).toEqual({ name: "job", jobId: id });
  });
});

describe("routeForNotification", () => {
  it("prefers an explicit job_id", () => {
    expect(routeForNotification({ job_id: "stripe:1" })).toEqual({
      name: "job",
      jobId: "stripe:1",
    });
  });

  it("falls back to the path inside an absolute deep_link", () => {
    expect(
      routeForNotification({ deep_link: "https://app.example.com/jobs/stripe%3A1" }),
    ).toEqual({ name: "job", jobId: "stripe:1" });
  });

  it("accepts a relative deep_link", () => {
    expect(routeForNotification({ deep_link: "/jobs/acme%3A9" })).toEqual({
      name: "job",
      jobId: "acme:9",
    });
  });

  it("uses job_ids when it names exactly one job", () => {
    expect(routeForNotification({ job_ids: "acme:9" })).toEqual({ name: "job", jobId: "acme:9" });
  });

  it("goes to the feed for a grouped alert", () => {
    // Several jobs: there is no single right job to open.
    expect(routeForNotification({ job_ids: "a:1,b:2,c:3", job_count: "3" })).toEqual({
      name: "feed",
    });
    expect(routeForNotification({ deep_link: "https://app.example.com/" })).toEqual({
      name: "feed",
    });
  });

  it("goes to the feed for an empty or missing payload", () => {
    expect(routeForNotification(null)).toEqual({ name: "feed" });
    expect(routeForNotification(undefined)).toEqual({ name: "feed" });
    expect(routeForNotification({})).toEqual({ name: "feed" });
  });

  it("ignores a blank job_id", () => {
    expect(routeForNotification({ job_id: "   " })).toEqual({ name: "feed" });
  });

  it("survives a nonsense deep_link", () => {
    expect(routeForNotification({ deep_link: "::::" })).toEqual({ name: "feed" });
  });
});

describe("parseNotificationPayload", () => {
  it("reads a flat data map", () => {
    expect(parseNotificationPayload({ job_id: "a:1", deep_link: "/jobs/a%3A1" })).toEqual({
      job_id: "a:1",
      deep_link: "/jobs/a%3A1",
    });
  });

  it("reads a nested Web Push payload", () => {
    const push = { title: "Stripe", body: "SWE Intern", data: { job_id: "stripe:1" } };
    expect(parseNotificationPayload(push)).toEqual({ job_id: "stripe:1" });
  });

  it("drops non-string values rather than trusting them", () => {
    expect(parseNotificationPayload({ job_id: 42, deep_link: "/jobs/x" })).toEqual({
      deep_link: "/jobs/x",
    });
  });

  it("returns null for unusable input", () => {
    expect(parseNotificationPayload(null)).toBeNull();
    expect(parseNotificationPayload("string")).toBeNull();
    expect(parseNotificationPayload({})).toBeNull();
    expect(parseNotificationPayload({ unrelated: "x" })).toBeNull();
  });
});
