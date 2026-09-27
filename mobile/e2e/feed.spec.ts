/**
 * Level 5 browser E2E (PRD §14 Level 5: "at least one end-to-end or
 * integration-level client test").
 *
 * This runs the built PWA in a real Chromium against a stub of the jobs API
 * served from the page itself (route interception), which keeps the spec
 * hermetic. The full cross-stack story - a scraped job becoming a backend record,
 * a notification payload, and a visible, deep-linkable item in this client - is
 * proved by `tests/e2e/test_acceptance.py::TestScenario8`, which drives the real
 * backend and asserts against the same contract these routes encode.
 */
import { expect, test, type Page } from "@playwright/test";

const JOBS = [
  {
    job_id: "anthropic:4020160008",
    company: "Anthropic",
    title: "Software Engineer Intern",
    url: "https://job-boards.greenhouse.io/anthropic/jobs/4020160008",
    source: "greenhouse",
    location: "San Francisco, CA",
    date_posted: "2026-09-18T00:00:00+00:00",
    first_seen: "2026-09-26T12:00:00+00:00",
    last_seen: "2026-09-26T12:00:00+00:00",
    relevance_score: 81,
    priority: "high",
    industry: "AI Labs",
    employment_type: "Intern",
    description: "Work on inference infrastructure.",
    notification_sent: true,
  },
  {
    job_id: "figma:9001",
    company: "Figma",
    title: "Backend Engineer Intern",
    url: "https://job-boards.greenhouse.io/figma/jobs/9001",
    source: "greenhouse",
    location: "New York, NY",
    date_posted: null,
    first_seen: "2026-09-26T11:00:00+00:00",
    last_seen: "2026-09-26T11:30:00+00:00",
    relevance_score: 73,
    priority: "high",
    industry: "Design Tools",
    employment_type: "Intern",
    description: null,
    notification_sent: true,
  },
];

/** True for requests aimed at the jobs API, never at the app's own documents. */
const isApiRequest = (url: URL) => url.hostname === "localhost" && url.port === "8000";
const isListRequest = (url: URL) => isApiRequest(url) && /^\/jobs\/?$/.test(url.pathname);
const isDetailRequest = (url: URL) => isApiRequest(url) && /^\/jobs\/.+/.test(url.pathname);

async function stubApi(page: Page, options: { jobs?: unknown[]; status?: number } = {}) {
  const jobs = options.jobs ?? JOBS;
  // Predicates rather than globs: a glob like '**/jobs/*' also matches the
  // app's own /jobs/<id> document navigation, which would serve JSON in place
  // of the HTML app and break exactly the deep-link case this file exists to test.
  await page.route(isListRequest, async (route) => {
    if (options.status && options.status !== 200) {
      await route.fulfill({ status: options.status, body: "{}" });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ count: jobs.length, limit: 50, jobs }),
    });
  });
  await page.route(isDetailRequest, async (route) => {
    const id = decodeURIComponent(new URL(route.request().url()).pathname.split("/jobs/")[1] ?? "");
    const found = (jobs as { job_id?: string }[]).find((job) => job.job_id === id);
    if (!found) {
      await route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ job: found }),
    });
  });
}

test.describe("the installed feed", () => {
  test("boots and lists recently discovered internships", async ({ page }) => {
    await stubApi(page);
    await page.goto("/");

    await expect(page.getByRole("heading", { name: "Internship Monitor" })).toBeVisible();
    const cards = page.getByTestId("job-card");
    await expect(cards).toHaveCount(2);
    await expect(cards.first()).toContainText("Anthropic");
    await expect(cards.first()).toContainText("Software Engineer Intern");
    await expect(cards.first()).toContainText("San Francisco, CA");
  });

  test("opening a job shows its detail and a working Apply link", async ({ page }) => {
    await stubApi(page);
    await page.goto("/");
    await page.getByTestId("job-card").first().click();

    const detail = page.getByTestId("job-detail");
    await expect(detail).toBeVisible();
    await expect(detail).toHaveAttribute("data-job-id", "anthropic:4020160008");

    const apply = page.getByTestId("apply-link");
    await expect(apply).toHaveAttribute(
      "href",
      "https://job-boards.greenhouse.io/anthropic/jobs/4020160008",
    );
    await expect(apply).toHaveAttribute("target", "_blank");
    // The URL is pushed, so the page is shareable and reloadable.
    await expect(page).toHaveURL(/\/jobs\/anthropic%3A4020160008$/);
  });

  test("a notification deep link loads that job directly", async ({ page }) => {
    // This is what tapping a push notification does: a cold load of the deep
    // link, with no feed fetched first. It only works because the deploy serves
    // index.html for unknown paths (SPA fallback).
    await stubApi(page);
    await page.goto("/jobs/figma%3A9001");

    const detail = page.getByTestId("job-detail");
    await expect(detail).toBeVisible();
    await expect(detail).toHaveAttribute("data-job-id", "figma:9001");
    await expect(detail).toContainText("Backend Engineer Intern");
    await expect(page.getByTestId("apply-link")).toHaveAttribute(
      "href",
      "https://job-boards.greenhouse.io/figma/jobs/9001",
    );
  });

  test("a notification message routes an already-open app to the right job", async ({ page }) => {
    await stubApi(page);
    await page.goto("/");
    await expect(page.getByTestId("job-list")).toBeVisible();

    // The payload shape the backend's format_push() produces.
    await page.evaluate(() => {
      window.dispatchEvent(
        new CustomEvent("jobmonitor:notification", {
          detail: {
            schema_version: "1",
            urgency: "immediate",
            job_id: "figma:9001",
            deep_link: "http://127.0.0.1:4173/jobs/figma%3A9001",
            apply_url: "https://job-boards.greenhouse.io/figma/jobs/9001",
          },
        }),
      );
    });

    await expect(page.getByTestId("job-detail")).toHaveAttribute("data-job-id", "figma:9001");
    await expect(page.getByTestId("banner")).toContainText("notification");
  });

  test("duplicate notifications do not duplicate visible items", async ({ page }) => {
    await stubApi(page);
    await page.goto("/");
    await expect(page.getByTestId("job-card")).toHaveCount(2);

    await page.evaluate(() => {
      for (let i = 0; i < 4; i += 1) {
        window.dispatchEvent(
          new CustomEvent("jobmonitor:notification", {
            detail: { job_ids: "anthropic:4020160008,figma:9001", job_count: "2" },
          }),
        );
      }
    });

    await expect(page.getByTestId("job-card")).toHaveCount(2);
    await expect(page.getByText("2 recent")).toBeVisible();
  });

  test("back navigation returns to the feed", async ({ page }) => {
    await stubApi(page);
    await page.goto("/");
    await page.getByTestId("job-card").first().click();
    await expect(page.getByTestId("job-detail")).toBeVisible();

    await page.goBack();
    await expect(page.getByTestId("job-list")).toBeVisible();
  });

  test("the empty state renders when nothing has been found", async ({ page }) => {
    await stubApi(page, { jobs: [] });
    await page.goto("/");
    await expect(page.getByTestId("empty-state")).toContainText("No internships yet");
  });

  test("the error state renders when the API is down", async ({ page }) => {
    await stubApi(page, { status: 503 });
    await page.goto("/");
    await expect(page.getByTestId("error-state")).toContainText("Could not load jobs");
  });

  test("malformed backend data does not blank the feed", async ({ page }) => {
    await stubApi(page, { jobs: [JOBS[0], { job_id: "broken" }, null, 42] });
    await page.goto("/");
    await expect(page.getByTestId("job-card")).toHaveCount(1);
  });

  test("the app is installable as a PWA", async ({ page }) => {
    await stubApi(page);
    await page.goto("/");
    const manifestHref = await page.getAttribute('link[rel="manifest"]', "href");
    expect(manifestHref).toBe("/manifest.webmanifest");

    const manifest = await page.request.get(manifestHref!);
    expect(manifest.ok()).toBeTruthy();
    const parsed = await manifest.json();
    expect(parsed.display).toBe("standalone");
    expect(parsed.start_url).toBe("/");
    expect(parsed.icons.length).toBeGreaterThan(0);
  });

  test("no console errors on a normal session", async ({ page }) => {
    const errors: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    await stubApi(page);
    await page.goto("/");
    await page.getByTestId("job-card").first().click();
    await expect(page.getByTestId("job-detail")).toBeVisible();
    expect(errors).toEqual([]);
  });
});
