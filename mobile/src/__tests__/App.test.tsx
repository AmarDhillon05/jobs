/**
 * Level 5 - the client (PRD §14 Level 5, §23).
 *
 * Covers every item the PRD lists: the app boots and renders, recent jobs load,
 * empty and error states render, job detail renders, the application URL action
 * works, a notification payload is accepted, tapping a notification routes to the
 * right job, duplicate events do not create duplicate visible items, and
 * malformed backend data fails gracefully.
 */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";

const job = (id: string, overrides: Record<string, unknown> = {}) => ({
  job_id: id,
  company: "Stripe",
  title: "Software Engineer Intern",
  url: `https://stripe.com/jobs/${id}`,
  source: "greenhouse",
  location: "San Francisco, CA",
  date_posted: "2026-09-18T00:00:00Z",
  first_seen: "2026-09-26T12:00:00Z",
  last_seen: "2026-09-26T12:00:00Z",
  relevance_score: 73,
  priority: "high",
  industry: "Payments",
  employment_type: "Intern",
  description: "Build payments infrastructure.",
  notification_sent: false,
  ...overrides,
});

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

/** Route stubbed fetches by URL, so list and detail can differ. */
function stubFetch(routes: {
  list?: unknown;
  detail?: Record<string, unknown>;
  listStatus?: number;
  detailStatus?: number;
  reject?: boolean;
}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (routes.reject) throw new TypeError("Failed to fetch");
    if (/\/jobs\/[^?]/.test(url)) {
      const id = decodeURIComponent(url.split("/jobs/")[1] ?? "");
      const found = routes.detail?.[id];
      if (routes.detailStatus && routes.detailStatus !== 200) {
        return jsonResponse({ error: {} }, routes.detailStatus);
      }
      if (!found) return jsonResponse({ error: {} }, 404);
      return jsonResponse({ job: found });
    }
    if (routes.listStatus && routes.listStatus !== 200) {
      return jsonResponse({ error: {} }, routes.listStatus);
    }
    return jsonResponse(routes.list ?? { count: 0, limit: 50, jobs: [] });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const navigate = vi.fn();

beforeEach(() => {
  navigate.mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("boot and render", () => {
  it("renders the shell immediately, before data arrives", () => {
    stubFetch({ list: { jobs: [] } });
    render(<App initialPath="/" navigate={navigate} />);
    expect(screen.getByRole("heading", { name: "Internship Monitor" })).toBeInTheDocument();
    expect(screen.getByTestId("loading-state")).toBeInTheDocument();
  });

  it("does not crash when rendered with no data at all", () => {
    stubFetch({ list: undefined });
    expect(() => render(<App initialPath="/" navigate={navigate} />)).not.toThrow();
  });
});

describe("loading recent jobs", () => {
  it("shows the jobs returned by the API", async () => {
    stubFetch({ list: { count: 2, limit: 50, jobs: [job("stripe:1"), job("stripe:2", { title: "ML Intern" })] } });
    render(<App initialPath="/" navigate={navigate} />);

    const list = await screen.findByTestId("job-list");
    const cards = within(list).getAllByTestId("job-card");
    expect(cards).toHaveLength(2);
    expect(cards[0]).toHaveTextContent("Stripe");
    expect(cards[0]).toHaveTextContent("Software Engineer Intern");
    expect(cards[0]).toHaveTextContent("San Francisco, CA");
  });

  it("shows the company, title, location and relevance for each job", async () => {
    stubFetch({ list: { jobs: [job("stripe:1")] } });
    render(<App initialPath="/" navigate={navigate} />);
    const card = await screen.findByTestId("job-card");
    expect(card).toHaveTextContent("73");
    expect(within(card).getByLabelText("Relevance 73 of 100")).toBeInTheDocument();
  });

  it("reports how many jobs are shown", async () => {
    stubFetch({ list: { jobs: [job("a:1"), job("a:2"), job("a:3")] } });
    render(<App initialPath="/" navigate={navigate} />);
    expect(await screen.findByText("3 recent")).toBeInTheDocument();
  });

  it("requests the recent-jobs endpoint", async () => {
    const fetchMock = stubFetch({ list: { jobs: [] } });
    render(<App initialPath="/" navigate={navigate} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("/jobs?limit=50");
  });
});

describe("empty state", () => {
  it("explains that nothing has been found yet", async () => {
    stubFetch({ list: { count: 0, limit: 50, jobs: [] } });
    render(<App initialPath="/" navigate={navigate} />);
    const empty = await screen.findByTestId("empty-state");
    expect(empty).toHaveTextContent("No internships yet");
    expect(empty).toHaveTextContent(/ten minutes/i);
  });

  it("offers a refresh that re-requests the data", async () => {
    const fetchMock = stubFetch({ list: { jobs: [] } });
    render(<App initialPath="/" navigate={navigate} />);
    await screen.findByTestId("empty-state");
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(1));
  });
});

describe("error state", () => {
  it("renders an alert when the network fails", async () => {
    stubFetch({ reject: true });
    render(<App initialPath="/" navigate={navigate} />);
    const error = await screen.findByTestId("error-state");
    expect(error).toHaveTextContent("Could not load jobs");
    expect(error).toHaveTextContent(/could not reach/i);
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("renders an error when the API returns 500", async () => {
    stubFetch({ listStatus: 500 });
    render(<App initialPath="/" navigate={navigate} />);
    expect(await screen.findByTestId("error-state")).toHaveTextContent("500");
  });

  it("can retry after an error", async () => {
    const fetchMock = stubFetch({ listStatus: 500 });
    render(<App initialPath="/" navigate={navigate} />);
    await screen.findByTestId("error-state");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(1));
  });
});

describe("malformed backend data", () => {
  it("keeps the usable jobs and drops the broken ones", async () => {
    stubFetch({
      list: { jobs: [job("ok:1"), { job_id: "broken" }, { nonsense: true }, null] },
    });
    render(<App initialPath="/" navigate={navigate} />);
    const cards = await screen.findAllByTestId("job-card");
    expect(cards).toHaveLength(1);
    expect(cards[0]).toHaveAttribute("data-job-id", "ok:1");
  });

  it("falls back to the empty state when every record is unusable", async () => {
    stubFetch({ list: { jobs: [{ job_id: "broken" }, "nope", 42] } });
    render(<App initialPath="/" navigate={navigate} />);
    expect(await screen.findByTestId("empty-state")).toBeInTheDocument();
  });

  it("does not render a job whose apply url is not http(s)", async () => {
    stubFetch({ list: { jobs: [job("evil:1", { url: "javascript:alert(1)" })] } });
    render(<App initialPath="/" navigate={navigate} />);
    expect(await screen.findByTestId("empty-state")).toBeInTheDocument();
  });

  it("survives a response that is not JSON at all", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => {
          throw new Error("not json");
        },
      }) as unknown as Response),
    );
    render(<App initialPath="/" navigate={navigate} />);
    expect(await screen.findByTestId("error-state")).toBeInTheDocument();
  });
});

describe("job detail", () => {
  it("opens when a job is tapped", async () => {
    stubFetch({ list: { jobs: [job("stripe:1")] }, detail: { "stripe:1": job("stripe:1") } });
    render(<App initialPath="/" navigate={navigate} />);
    await userEvent.click(await screen.findByTestId("job-card"));

    const detail = await screen.findByTestId("job-detail");
    expect(detail).toHaveAttribute("data-job-id", "stripe:1");
    expect(navigate).toHaveBeenCalledWith("/jobs/stripe%3A1");
  });

  it("shows everything the PRD's detail screen requires", async () => {
    stubFetch({ list: { jobs: [job("stripe:1")] } });
    render(<App initialPath="/" navigate={navigate} />);
    await userEvent.click(await screen.findByTestId("job-card"));

    const detail = await screen.findByTestId("job-detail");
    expect(detail).toHaveTextContent("Stripe");
    expect(detail).toHaveTextContent("Software Engineer Intern");
    expect(screen.getByTestId("detail-location")).toHaveTextContent("San Francisco, CA");
    expect(detail).toHaveTextContent("2026-09-18");
    expect(screen.getByTestId("detail-first-seen")).toHaveTextContent("2026-09-26");
    expect(screen.getByTestId("detail-description")).toHaveTextContent("Build payments");
    expect(detail).toHaveTextContent("73/100");
  });

  it("loads the job by id when deep-linked with no feed loaded", async () => {
    // The notification path: the app opens straight onto a detail screen.
    stubFetch({ list: { jobs: [] }, detail: { "stripe:1": job("stripe:1") } });
    render(<App initialPath="/jobs/stripe%3A1" navigate={navigate} />);
    expect(await screen.findByTestId("job-detail")).toHaveAttribute("data-job-id", "stripe:1");
  });

  it("shows an error when the deep-linked job is gone", async () => {
    stubFetch({ list: { jobs: [] }, detail: {} });
    render(<App initialPath="/jobs/ghost%3A1" navigate={navigate} />);
    const error = await screen.findByTestId("error-state");
    expect(error).toHaveTextContent("no longer available");
  });

  it("can go back to the feed", async () => {
    stubFetch({ list: { jobs: [job("stripe:1")] } });
    render(<App initialPath="/" navigate={navigate} />);
    await userEvent.click(await screen.findByTestId("job-card"));
    await userEvent.click(await screen.findByTestId("back"));
    expect(await screen.findByTestId("job-list")).toBeInTheDocument();
    expect(navigate).toHaveBeenLastCalledWith("/");
  });

  it("handles a location-less job without rendering 'null'", async () => {
    stubFetch({ list: { jobs: [job("stripe:1", { location: null })] } });
    render(<App initialPath="/" navigate={navigate} />);
    await userEvent.click(await screen.findByTestId("job-card"));
    expect(await screen.findByTestId("detail-location")).toHaveTextContent("Not specified");
  });
});

describe("the application URL action", () => {
  it("links to the original posting, unchanged", async () => {
    stubFetch({ list: { jobs: [job("stripe:1")] } });
    render(<App initialPath="/" navigate={navigate} />);
    await userEvent.click(await screen.findByTestId("job-card"));

    const apply = await screen.findByTestId("apply-link");
    expect(apply).toHaveAttribute("href", "https://stripe.com/jobs/stripe:1");
    expect(apply).toHaveTextContent("Open Application");
  });

  it("opens in a new tab without leaking the referrer", async () => {
    stubFetch({ list: { jobs: [job("stripe:1")] } });
    render(<App initialPath="/" navigate={navigate} />);
    await userEvent.click(await screen.findByTestId("job-card"));
    const apply = await screen.findByTestId("apply-link");
    expect(apply).toHaveAttribute("target", "_blank");
    expect(apply.getAttribute("rel")).toContain("noopener");
    expect(apply.getAttribute("rel")).toContain("noreferrer");
  });

  it("is clickable", async () => {
    stubFetch({ list: { jobs: [job("stripe:1")] } });
    render(<App initialPath="/" navigate={navigate} />);
    await userEvent.click(await screen.findByTestId("job-card"));
    const apply = await screen.findByTestId("apply-link");
    // jsdom does not navigate; assert the click is not swallowed or prevented.
    const clicked = apply.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    expect(clicked).toBe(true);
  });
});

describe("notification handling", () => {
  /** Wrapped in act(): the listener updates state synchronously. */
  function dispatchNotification(payload: unknown) {
    act(() => {
      window.dispatchEvent(new CustomEvent("jobmonitor:notification", { detail: payload }));
    });
  }

  it("accepts a notification payload and routes to the named job", async () => {
    stubFetch({ list: { jobs: [job("stripe:1"), job("stripe:2")] }, detail: { "stripe:2": job("stripe:2") } });
    render(<App initialPath="/" navigate={navigate} />);
    await screen.findByTestId("job-list");

    dispatchNotification({ job_id: "stripe:2", deep_link: "https://app.test/jobs/stripe%3A2" });

    const detail = await screen.findByTestId("job-detail");
    expect(detail).toHaveAttribute("data-job-id", "stripe:2");
    expect(navigate).toHaveBeenCalledWith("/jobs/stripe%3A2");
  });

  it("routes using deep_link alone", async () => {
    stubFetch({ list: { jobs: [job("stripe:1")] }, detail: { "stripe:1": job("stripe:1") } });
    render(<App initialPath="/" navigate={navigate} />);
    await screen.findByTestId("job-list");

    dispatchNotification({ deep_link: "https://app.test/jobs/stripe%3A1" });
    expect(await screen.findByTestId("job-detail")).toHaveAttribute("data-job-id", "stripe:1");
  });

  it("shows the feed for a grouped alert", async () => {
    stubFetch({ list: { jobs: [job("a:1"), job("a:2")] } });
    render(<App initialPath="/" navigate={navigate} />);
    await screen.findByTestId("job-list");

    dispatchNotification({ job_ids: "a:1,a:2", job_count: "2", deep_link: "https://app.test/" });

    expect(await screen.findByTestId("banner")).toHaveTextContent("New internships found");
    expect(screen.getByTestId("job-list")).toBeInTheDocument();
  });

  it("tolerates an empty or malformed payload", async () => {
    stubFetch({ list: { jobs: [job("a:1")] } });
    render(<App initialPath="/" navigate={navigate} />);
    await screen.findByTestId("job-list");

    dispatchNotification(null);
    dispatchNotification({ job_id: 42 });
    dispatchNotification("garbage");

    expect(await screen.findByTestId("job-list")).toBeInTheDocument();
  });

  it("refetches when a notification arrives, because the backend has new data", async () => {
    const fetchMock = stubFetch({ list: { jobs: [job("a:1")] } });
    render(<App initialPath="/" navigate={navigate} />);
    await screen.findByTestId("job-list");
    const before = fetchMock.mock.calls.length;

    dispatchNotification({ job_ids: "a:1,a:2", job_count: "2" });
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(before));
  });

  it("duplicate notification events do not create duplicate visible items", async () => {
    stubFetch({ list: { count: 1, limit: 50, jobs: [job("a:1")] } });
    render(<App initialPath="/" navigate={navigate} />);
    await screen.findByTestId("job-list");

    // The same alert delivered three times - a real possibility with at-least-once
    // queue delivery plus a service-worker relay.
    dispatchNotification({ job_ids: "a:1", job_count: "1", deep_link: "https://app.test/" });
    dispatchNotification({ job_ids: "a:1", job_count: "1", deep_link: "https://app.test/" });
    dispatchNotification({ job_ids: "a:1", job_count: "1", deep_link: "https://app.test/" });

    await waitFor(() => expect(screen.getAllByTestId("job-card")).toHaveLength(1));
    expect(screen.getByText("1 recent")).toBeInTheDocument();
  });

  it("repeated list loads never duplicate a job", async () => {
    stubFetch({ list: { jobs: [job("a:1"), job("a:2")] } });
    render(<App initialPath="/" navigate={navigate} />);
    await screen.findByTestId("job-list");

    await userEvent.click(screen.getByTestId("refresh"));
    await userEvent.click(screen.getByTestId("refresh"));

    await waitFor(() => expect(screen.getAllByTestId("job-card")).toHaveLength(2));
  });
});
