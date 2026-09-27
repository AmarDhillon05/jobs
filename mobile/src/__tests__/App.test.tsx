/**
 * Level 5 - the Expo client (PRD §14 Level 5, §23).
 *
 * Covers every item the PRD lists: the app boots and renders, recent jobs load,
 * empty and error states render, job detail renders, the application URL action
 * works, a notification payload is accepted, tapping a notification routes to the
 * right job, duplicate events do not create duplicate visible items, and malformed
 * backend data fails gracefully.
 *
 * `expo-notifications` is mocked (jest.setup.ts) because it wraps native modules
 * that do not exist in Node. What that means precisely: these tests prove the
 * app's *handling* - permission flow, token registration, listener wiring, tap
 * routing, cold-start replay - and not that the OS delivers a push. Delivery needs
 * a physical device; see mobile/README.md.
 */
import { act, render, screen, userEvent, waitFor } from "@testing-library/react-native";
import * as Notifications from "expo-notifications";

import App from "../App";

const job = (id: string, overrides: Record<string, unknown> = {}) => ({
  job_id: id,
  company: "Stripe",
  title: "Software Engineer Intern",
  url: `https://stripe.com/jobs/${id}`,
  source: "greenhouse",
  location: "San Francisco, CA",
  date_posted: "2026-09-18T00:00:00+00:00",
  first_seen: "2026-09-26T12:00:00+00:00",
  last_seen: "2026-09-26T12:00:00+00:00",
  relevance_score: 73,
  priority: "high",
  industry: "Payments",
  employment_type: "Intern",
  description: "Build payments infrastructure.",
  notification_sent: false,
  ...overrides,
});

interface StubOptions {
  list?: unknown[];
  detail?: Record<string, unknown>;
  listStatus?: number;
  reject?: boolean;
  badJson?: boolean;
}

function stubFetch(options: StubOptions = {}) {
  const jobs = options.list ?? [];
  const fetchMock = jest.fn(async (input: string) => {
    if (options.reject) throw new TypeError("Network request failed");
    const url = String(input);

    const ok = (body: unknown, status = 200) =>
      ({
        ok: status >= 200 && status < 300,
        status,
        json: async () => {
          if (options.badJson) throw new Error("not json");
          return body;
        },
      }) as Response;

    if (/\/jobs\/[^?]/.test(url)) {
      const id = decodeURIComponent(url.split("/jobs/")[1] ?? "");
      const found = options.detail?.[id];
      return found ? ok({ job: found }) : ok({}, 404);
    }
    if (options.listStatus && options.listStatus !== 200) return ok({}, options.listStatus);
    return ok({ count: jobs.length, limit: 50, jobs });
  });
  global.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

/** Push registration is injected so no test depends on the native module. */
const registerPush = jest.fn(async () => ({ status: "registered" as const, token: "ExponentPushToken[x]" }));

beforeEach(() => {
  jest.clearAllMocks();
  registerPush.mockResolvedValue({ status: "registered", token: "ExponentPushToken[x]" });
  (Notifications.getLastNotificationResponseAsync as jest.Mock).mockResolvedValue(null);
});

/** Capture the listener the app registers, so a tap can be simulated. */
function tapListener(): (response: unknown) => void {
  const mock = Notifications.addNotificationResponseReceivedListener as jest.Mock;
  expect(mock).toHaveBeenCalled();
  return mock.mock.calls[0][0] as (response: unknown) => void;
}

function receivedListener(): (notification: unknown) => void {
  const mock = Notifications.addNotificationReceivedListener as jest.Mock;
  return mock.mock.calls[0][0] as (notification: unknown) => void;
}

function pushResponse(data: Record<string, string>) {
  return { notification: { request: { content: { data } } } };
}

describe("boot and render", () => {
  it("renders the masthead before any data arrives", () => {
    stubFetch();
    render(<App registerPush={registerPush} />);
    expect(screen.getByText("Internship Monitor")).toBeTruthy();
    expect(screen.getByTestId("loading-state")).toBeTruthy();
  });

  it("configures foreground notification behaviour on boot", () => {
    stubFetch();
    render(<App registerPush={registerPush} />);
    expect(Notifications.setNotificationHandler).toHaveBeenCalled();
  });

  it("registers for push notifications on boot", async () => {
    stubFetch();
    render(<App registerPush={registerPush} />);
    await waitFor(() => expect(registerPush).toHaveBeenCalled());
  });

  it("does not crash when the API returns nothing usable", async () => {
    stubFetch({ list: [] });
    render(<App registerPush={registerPush} />);
    expect(await screen.findByTestId("empty-state")).toBeTruthy();
  });
});

describe("loading recent jobs", () => {
  it("lists the jobs the API returns", async () => {
    stubFetch({ list: [job("stripe:1"), job("stripe:2", { title: "ML Intern" })] });
    render(<App registerPush={registerPush} />);

    expect(await screen.findByTestId("job-list")).toBeTruthy();
    expect(screen.getByTestId("job-card-stripe:1")).toBeTruthy();
    expect(screen.getByTestId("job-card-stripe:2")).toBeTruthy();
    expect(screen.getAllByText("Stripe").length).toBeGreaterThan(0);
  });

  it("shows location and relevance for each job", async () => {
    stubFetch({ list: [job("stripe:1")] });
    render(<App registerPush={registerPush} />);
    await screen.findByTestId("job-list");
    expect(screen.getByText("San Francisco, CA")).toBeTruthy();
    expect(screen.getByText("73")).toBeTruthy();
  });

  it("reports how many jobs are shown", async () => {
    stubFetch({ list: [job("a:1"), job("a:2"), job("a:3")] });
    render(<App registerPush={registerPush} />);
    expect(await screen.findByText("3 recent")).toBeTruthy();
  });

  it("requests the recent-jobs endpoint", async () => {
    const fetchMock = stubFetch({ list: [] });
    render(<App registerPush={registerPush} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("/jobs?limit=50");
  });
});

describe("empty and error states", () => {
  it("explains the empty state", async () => {
    stubFetch({ list: [] });
    render(<App registerPush={registerPush} />);
    const empty = await screen.findByTestId("empty-state");
    expect(empty).toBeTruthy();
    expect(screen.getByText("No internships yet")).toBeTruthy();
    expect(screen.getByText(/ten minutes/i)).toBeTruthy();
  });

  it("renders an alert when the network fails", async () => {
    stubFetch({ reject: true });
    render(<App registerPush={registerPush} />);
    expect(await screen.findByTestId("error-state")).toBeTruthy();
    expect(screen.getByText(/could not reach/i)).toBeTruthy();
  });

  it("renders an error when the API returns 500", async () => {
    stubFetch({ listStatus: 500 });
    render(<App registerPush={registerPush} />);
    expect(await screen.findByTestId("error-state")).toBeTruthy();
    expect(screen.getByText(/500/)).toBeTruthy();
  });

  it("renders an error when the response is not readable JSON", async () => {
    stubFetch({ badJson: true });
    render(<App registerPush={registerPush} />);
    expect(await screen.findByTestId("error-state")).toBeTruthy();
  });

  it("can retry after an error", async () => {
    const fetchMock = stubFetch({ listStatus: 500 });
    render(<App registerPush={registerPush} />);
    await screen.findByTestId("error-state");
    const before = fetchMock.mock.calls.length;
    await userEvent.press(screen.getByTestId("error-action"));
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(before));
  });

  it("surfaces a notice when push registration did not succeed", async () => {
    stubFetch({ list: [] });
    registerPush.mockResolvedValue({
      status: "denied",
      detail: "Notification permission was not granted.",
    } as never);
    render(<App registerPush={registerPush} />);
    expect(await screen.findByTestId("push-notice")).toBeTruthy();
    expect(screen.getByText(/permission was not granted/i)).toBeTruthy();
  });
});

describe("malformed backend data", () => {
  it("keeps the usable jobs and drops the broken ones", async () => {
    stubFetch({ list: [job("ok:1"), { job_id: "broken" }, null, 42] });
    render(<App registerPush={registerPush} />);
    await screen.findByTestId("job-list");
    expect(screen.getByTestId("job-card-ok:1")).toBeTruthy();
    expect(screen.queryByTestId("job-card-broken")).toBeNull();
    expect(screen.getByText("1 recent")).toBeTruthy();
  });

  it("refuses a job whose apply url is not http(s)", async () => {
    // A javascript: or custom-scheme URL from a compromised upstream board must
    // never become a tappable button.
    stubFetch({ list: [job("evil:1", { url: "javascript:alert(1)" })] });
    render(<App registerPush={registerPush} />);
    expect(await screen.findByTestId("empty-state")).toBeTruthy();
  });

  it("falls back to the empty state when every record is unusable", async () => {
    stubFetch({ list: [{ job_id: "broken" }, "nope", 42] });
    render(<App registerPush={registerPush} />);
    expect(await screen.findByTestId("empty-state")).toBeTruthy();
  });
});

describe("job detail", () => {
  it("opens when a job is tapped", async () => {
    stubFetch({ list: [job("stripe:1")], detail: { "stripe:1": job("stripe:1") } });
    render(<App registerPush={registerPush} />);
    await userEvent.press(await screen.findByTestId("job-card-stripe:1"));
    expect(await screen.findByTestId("job-detail-stripe:1")).toBeTruthy();
  });

  it("shows everything the PRD's detail screen requires", async () => {
    stubFetch({ list: [job("stripe:1")] });
    render(<App registerPush={registerPush} />);
    await userEvent.press(await screen.findByTestId("job-card-stripe:1"));
    await screen.findByTestId("job-detail-stripe:1");

    expect(screen.getByText("Software Engineer Intern")).toBeTruthy();
    expect(screen.getByText("Stripe")).toBeTruthy();
    expect(screen.getByTestId("detail-location")).toHaveTextContent("San Francisco, CA");
    expect(screen.getByTestId("detail-first-seen")).toHaveTextContent(/2026-09-26/);
    expect(screen.getByTestId("detail-description")).toHaveTextContent(/Build payments/);
    expect(screen.getByText("73/100")).toBeTruthy();
  });

  it("fetches the job by id when deep-linked with no feed loaded", async () => {
    stubFetch({ list: [], detail: { "stripe:1": job("stripe:1") } });
    render(<App initialRoute={{ name: "job", jobId: "stripe:1" }} registerPush={registerPush} />);
    expect(await screen.findByTestId("job-detail-stripe:1")).toBeTruthy();
  });

  it("shows an error when the deep-linked job is gone", async () => {
    stubFetch({ list: [], detail: {} });
    render(<App initialRoute={{ name: "job", jobId: "ghost:1" }} registerPush={registerPush} />);
    expect(await screen.findByText(/no longer available/i)).toBeTruthy();
  });

  it("can go back to the feed", async () => {
    stubFetch({ list: [job("stripe:1")] });
    render(<App registerPush={registerPush} />);
    await userEvent.press(await screen.findByTestId("job-card-stripe:1"));
    await userEvent.press(await screen.findByTestId("back"));
    expect(await screen.findByTestId("job-list")).toBeTruthy();
  });

  it("renders a location-less job without printing null", async () => {
    stubFetch({ list: [job("stripe:1", { location: null })] });
    render(<App registerPush={registerPush} />);
    await userEvent.press(await screen.findByTestId("job-card-stripe:1"));
    expect(await screen.findByTestId("detail-location")).toHaveTextContent("Not specified");
  });
});

describe("the application URL action", () => {
  it("opens the original posting, unchanged", async () => {
    const openUrl = jest.fn(async () => true);
    stubFetch({ list: [job("stripe:1")] });
    render(<App registerPush={registerPush} openUrl={openUrl} />);
    await userEvent.press(await screen.findByTestId("job-card-stripe:1"));
    await userEvent.press(await screen.findByTestId("apply-button"));
    expect(openUrl).toHaveBeenCalledWith("https://stripe.com/jobs/stripe:1");
  });

  it("shows the destination so the user can see where they are going", async () => {
    stubFetch({ list: [job("stripe:1")] });
    render(<App registerPush={registerPush} />);
    await userEvent.press(await screen.findByTestId("job-card-stripe:1"));
    expect(await screen.findByTestId("apply-url")).toHaveTextContent("https://stripe.com/jobs/stripe:1");
  });

  it("preserves query parameters in the apply URL", async () => {
    const openUrl = jest.fn(async () => true);
    const url = "https://boards.greenhouse.io/x/jobs/1?gh_jid=42";
    stubFetch({ list: [job("stripe:1", { url })] });
    render(<App registerPush={registerPush} openUrl={openUrl} />);
    await userEvent.press(await screen.findByTestId("job-card-stripe:1"));
    await userEvent.press(await screen.findByTestId("apply-button"));
    expect(openUrl).toHaveBeenCalledWith(url);
  });
});

describe("notification handling", () => {
  it("accepts a push payload and routes to the named job", async () => {
    stubFetch({
      list: [job("stripe:1"), job("stripe:2")],
      detail: { "stripe:2": job("stripe:2") },
    });
    render(<App registerPush={registerPush} />);
    await screen.findByTestId("job-list");

    await act(async () => {
      tapListener()(
        pushResponse({
          job_id: "stripe:2",
          deep_link: "https://app.test/jobs/stripe%3A2",
          apply_url: "https://stripe.com/jobs/stripe:2",
        }),
      );
    });

    expect(await screen.findByTestId("job-detail-stripe:2")).toBeTruthy();
    expect(screen.getByTestId("banner")).toHaveTextContent(/notification/);
  });

  it("routes using deep_link alone", async () => {
    stubFetch({ list: [job("stripe:1")], detail: { "stripe:1": job("stripe:1") } });
    render(<App registerPush={registerPush} />);
    await screen.findByTestId("job-list");

    await act(async () => {
      tapListener()(pushResponse({ deep_link: "https://app.test/jobs/stripe%3A1" }));
    });
    expect(await screen.findByTestId("job-detail-stripe:1")).toBeTruthy();
  });

  it("routes using a custom-scheme deep link", async () => {
    stubFetch({ list: [job("stripe:1")], detail: { "stripe:1": job("stripe:1") } });
    render(<App registerPush={registerPush} />);
    await screen.findByTestId("job-list");

    await act(async () => {
      tapListener()(pushResponse({ deep_link: "internshipmonitor://jobs/stripe%3A1" }));
    });
    expect(await screen.findByTestId("job-detail-stripe:1")).toBeTruthy();
  });

  it("shows the feed for a grouped alert", async () => {
    stubFetch({ list: [job("a:1"), job("a:2")] });
    render(<App registerPush={registerPush} />);
    await screen.findByTestId("job-list");

    await act(async () => {
      tapListener()(
        pushResponse({ job_ids: "a:1,a:2", job_count: "2", deep_link: "https://app.test/" }),
      );
    });
    expect(screen.getByTestId("banner")).toHaveTextContent(/New internships found/);
    expect(screen.getByTestId("job-list")).toBeTruthy();
  });

  it("replays a cold-start tap that launched the app", async () => {
    // The app was not running; the OS delivers the response on startup.
    (Notifications.getLastNotificationResponseAsync as jest.Mock).mockResolvedValue(
      pushResponse({ job_id: "stripe:1" }),
    );
    stubFetch({ list: [], detail: { "stripe:1": job("stripe:1") } });
    render(<App registerPush={registerPush} />);
    expect(await screen.findByTestId("job-detail-stripe:1")).toBeTruthy();
  });

  it("refreshes the feed when a notification arrives in the foreground", async () => {
    const fetchMock = stubFetch({ list: [job("a:1")] });
    render(<App registerPush={registerPush} />);
    await screen.findByTestId("job-list");
    const before = fetchMock.mock.calls.length;

    await act(async () => {
      receivedListener()(pushResponse({ job_id: "a:1" }));
    });
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(before));
    expect(screen.getByTestId("banner")).toHaveTextContent(/New internship found/);
  });

  it("tolerates an empty or malformed payload", async () => {
    stubFetch({ list: [job("a:1")] });
    render(<App registerPush={registerPush} />);
    await screen.findByTestId("job-list");

    await act(async () => {
      const tap = tapListener();
      tap(pushResponse({}));
      tap({ notification: null });
      tap(null);
    });
    expect(screen.getByTestId("job-list")).toBeTruthy();
  });

  it("duplicate notification events do not create duplicate visible items", async () => {
    stubFetch({ list: [job("a:1")] });
    render(<App registerPush={registerPush} />);
    await screen.findByTestId("job-list");

    // The same alert delivered repeatedly - a real possibility with at-least-once
    // queue delivery plus an OS that may replay a tap.
    await act(async () => {
      const tap = tapListener();
      for (let index = 0; index < 4; index += 1) {
        tap(pushResponse({ job_ids: "a:1", job_count: "1", deep_link: "https://app.test/" }));
      }
    });

    // Four taps, one job on screen - not four stacked detail views.
    await waitFor(() => expect(screen.getAllByTestId("job-detail-a:1")).toHaveLength(1));

    // And the feed behind it still holds exactly one card, even though every tap
    // also triggered a refetch of the same job.
    await userEvent.press(screen.getByTestId("back"));
    await waitFor(() => expect(screen.getByText("1 recent")).toBeTruthy());
    expect(screen.getAllByTestId("job-card-a:1")).toHaveLength(1);
  });

  it("repeated refreshes never duplicate a job", async () => {
    stubFetch({ list: [job("a:1"), job("a:2")] });
    render(<App registerPush={registerPush} />);
    await screen.findByTestId("job-list");

    await act(async () => {
      receivedListener()(pushResponse({}));
      receivedListener()(pushResponse({}));
    });
    await waitFor(() => expect(screen.getByText("2 recent")).toBeTruthy());
  });

  it("removes its listeners on unmount", () => {
    stubFetch({ list: [] });
    const remove = jest.fn();
    (Notifications.addNotificationResponseReceivedListener as jest.Mock).mockReturnValue({ remove });
    (Notifications.addNotificationReceivedListener as jest.Mock).mockReturnValue({ remove });
    const view = render(<App registerPush={registerPush} />);
    view.unmount();
    expect(remove).toHaveBeenCalled();
  });
});
