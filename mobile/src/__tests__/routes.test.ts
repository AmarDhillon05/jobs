/**
 * Notification payload -> screen. The one path that must not break.
 *
 * Job ids contain a colon (`stripe:1`), so the backend percent-encodes them into
 * deep links; getting that pair wrong is exactly the kind of fault PRD §18.2 uses
 * as its worked example ("push notification not visible -> ... -> job ID
 * serialization"), which is why it is tested here directly rather than only
 * through the app.
 */
import { jobIdFromPath, parseNotificationPayload, routeForNotification } from "../routes";

describe("routeForNotification", () => {
  it("prefers an explicit job_id", () => {
    expect(routeForNotification({ job_id: "stripe:1", deep_link: "https://a/jobs/other%3A2" })).toEqual(
      { name: "job", jobId: "stripe:1" },
    );
  });

  it("falls back to the job id inside the deep link", () => {
    expect(routeForNotification({ deep_link: "https://app.test/jobs/stripe%3A1" })).toEqual({
      name: "job",
      jobId: "stripe:1",
    });
  });

  it("accepts the app's own scheme", () => {
    expect(routeForNotification({ deep_link: "internshipmonitor://jobs/ramp%3A9" })).toEqual({
      name: "job",
      jobId: "ramp:9",
    });
  });

  it("routes a single-id batch to that job", () => {
    expect(routeForNotification({ job_ids: "datadog:7" })).toEqual({
      name: "job",
      jobId: "datadog:7",
    });
  });

  it("routes a grouped alert to the feed", () => {
    expect(routeForNotification({ job_ids: "a:1,b:2,c:3", job_count: "3" })).toEqual({
      name: "feed",
    });
  });

  it.each([null, undefined, {}, { job_id: "   " }, { deep_link: "https://app.test/" }])(
    "routes %p to the feed rather than a broken detail screen",
    (payload) => {
      expect(routeForNotification(payload)).toEqual({ name: "feed" });
    },
  );

  it("trims a padded job id", () => {
    expect(routeForNotification({ job_id: "  stripe:1 " })).toEqual({
      name: "job",
      jobId: "stripe:1",
    });
  });
});

describe("jobIdFromPath", () => {
  it.each([
    ["https://app.test/jobs/stripe%3A1", "stripe:1"],
    ["https://app.test/jobs/stripe%3A1?from=push", "stripe:1"],
    ["https://app.test/jobs/stripe%3A1#top", "stripe:1"],
    ["internshipmonitor://jobs/stripe%3A1", "stripe:1"],
    ["/jobs/plain-id", "plain-id"],
  ])("reads %s", (link, expected) => {
    expect(jobIdFromPath(link)).toBe(expected);
  });

  it.each(["https://app.test/", "https://app.test/jobs", "not a url", ""])(
    "returns null for %p",
    (link) => {
      expect(jobIdFromPath(link)).toBeNull();
    },
  );

  it("keeps the raw segment when the escape sequence is malformed", () => {
    // decodeURIComponent throws on a lone %; a bad link must not crash a tap.
    expect(jobIdFromPath("https://app.test/jobs/bad%ZZ")).toBe("bad%ZZ");
  });
});

describe("parseNotificationPayload", () => {
  it("reads the Expo shape", () => {
    const raw = { request: { content: { data: { job_id: "a:1", urgency: "immediate" } } } };
    expect(parseNotificationPayload(raw)).toEqual({ job_id: "a:1", urgency: "immediate" });
  });

  it("reads a bare data wrapper", () => {
    expect(parseNotificationPayload({ data: { job_id: "a:1" } })).toEqual({ job_id: "a:1" });
  });

  it("reads a flat map", () => {
    expect(parseNotificationPayload({ job_id: "a:1" })).toEqual({ job_id: "a:1" });
  });

  it("drops unknown and non-string fields", () => {
    expect(parseNotificationPayload({ job_id: "a:1", nonsense: 1, job_count: 3 })).toEqual({
      job_id: "a:1",
    });
  });

  it.each([null, undefined, 7, "text", {}, { data: null }])("returns null for %p", (raw) => {
    expect(parseNotificationPayload(raw)).toBeNull();
  });

  it("carries the whole documented payload through", () => {
    const data = {
      schema_version: "1",
      urgency: "immediate",
      deep_link: "https://app.test/jobs/a%3A1",
      job_id: "a:1",
      job_ids: "a:1",
      job_count: "1",
      apply_url: "https://stripe.com/jobs/a1",
    };
    expect(parseNotificationPayload({ request: { content: { data } } })).toEqual(data);
  });
});
