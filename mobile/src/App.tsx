import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, fetchJob, fetchRecentJobs } from "./api";
import { JobDetail } from "./components/JobDetail";
import { JobList } from "./components/JobList";
import { EmptyState, ErrorState, LoadingState } from "./components/States";
import { jobPath, parseRoute, routeForNotification, type Route } from "./routes";
import type { Job, NotificationPayload } from "./types";
import { mergeJobs } from "./validate";

export interface AppProps {
  /** Starting path. Defaults to the browser's current location. */
  initialPath?: string;
  /** Pushed into history on navigation. Overridden in tests. */
  navigate?: (path: string) => void;
}

/**
 * The whole client.
 *
 * Two screens - a feed of recently discovered jobs and a job detail view - plus
 * the notification entry point. Navigation is a tiny path-based router rather
 * than a dependency, because the only route that has to work is the deep link a
 * push notification produces (`/jobs/<url-encoded id>`).
 */
export default function App({ initialPath, navigate }: AppProps = {}) {
  const [route, setRoute] = useState<Route>(() =>
    parseRoute(initialPath ?? (typeof window !== "undefined" ? window.location.pathname : "/")),
  );
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [banner, setBanner] = useState<string | null>(null);

  const go = useCallback(
    (path: string) => {
      setRoute(parseRoute(path));
      if (navigate) navigate(path);
      else if (typeof window !== "undefined" && window.history?.pushState) {
        window.history.pushState({}, "", path);
      }
    },
    [navigate],
  );

  // ---------------------------------------------------------------- the feed
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setListError(null);
    fetchRecentJobs({ signal: controller.signal })
      .then((response) => {
        if (!active) return;
        // Merge rather than replace: a job already shown (e.g. arrived via a
        // push while the feed was open) must not be duplicated.
        setJobs((current) => mergeJobs(current ?? [], response.jobs));
      })
      .catch((error: unknown) => {
        if (!active) return;
        if (error instanceof ApiError && error.message === "Request cancelled") return;
        setListError(error instanceof Error ? error.message : "Something went wrong.");
        setJobs((current) => current ?? []);
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [reloadToken]);

  // ------------------------------------------------- browser back / forward
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onPop = () => setRoute(parseRoute(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // ------------------------------------- notifications (tap or live message)
  useEffect(() => {
    if (typeof window === "undefined") return;

    const handle = (payload: NotificationPayload | null, source: string) => {
      const target = routeForNotification(payload);
      if (target.name === "job") {
        go(jobPath(target.jobId));
        setBanner(`Opened from ${source}`);
      } else {
        go("/");
        setBanner(`New internships found — showing the latest (${source})`);
      }
      // Re-fetch: a notification means the backend has something we do not.
      setReloadToken((token) => token + 1);
    };

    const onAppNotification = (event: Event) => {
      const detail = (event as CustomEvent).detail;
      handle(detail as NotificationPayload | null, "notification");
    };
    window.addEventListener("jobmonitor:notification", onAppNotification);

    // A service worker relays `notificationclick` to open pages this way.
    const onServiceWorkerMessage = (event: MessageEvent) => {
      const data = event.data as { type?: string; payload?: NotificationPayload } | null;
      if (data?.type === "jobmonitor:notification") handle(data.payload ?? null, "notification");
    };
    navigator.serviceWorker?.addEventListener?.("message", onServiceWorkerMessage);

    return () => {
      window.removeEventListener("jobmonitor:notification", onAppNotification);
      navigator.serviceWorker?.removeEventListener?.("message", onServiceWorkerMessage);
    };
  }, [go]);

  const selected = useMemo(
    () => (route.name === "job" ? jobs?.find((job) => job.job_id === route.jobId) : undefined),
    [route, jobs],
  );

  return (
    <div className="app">
      <header className="masthead">
        <h1>Internship Monitor</h1>
        {jobs && <span className="count">{jobs.length} recent</span>}
      </header>

      {banner && (
        <div className="banner" role="status" data-testid="banner">
          {banner}
        </div>
      )}

      {route.name === "job" ? (
        <JobScreen
          jobId={route.jobId}
          preloaded={selected}
          onBack={() => go("/")}
        />
      ) : (
        <FeedScreen
          jobs={jobs}
          error={listError}
          onOpen={(jobId) => go(jobPath(jobId))}
          onRetry={() => setReloadToken((token) => token + 1)}
        />
      )}
    </div>
  );
}

function FeedScreen({
  jobs,
  error,
  onOpen,
  onRetry,
}: {
  jobs: Job[] | null;
  error: string | null;
  onOpen: (jobId: string) => void;
  onRetry: () => void;
}) {
  if (error) {
    return (
      <ErrorState title="Could not load jobs" onRetry={onRetry}>
        <p>{error}</p>
      </ErrorState>
    );
  }
  if (jobs === null) return <LoadingState />;
  if (jobs.length === 0) {
    return (
      <EmptyState title="No internships yet">
        <p>
          Nothing has been discovered in the last poll. New roles appear here within about ten
          minutes of being posted.
        </p>
        <p>
          <button onClick={onRetry}>Refresh</button>
        </p>
      </EmptyState>
    );
  }
  return (
    <>
      <div className="toolbar">
        <button onClick={onRetry} data-testid="refresh">
          Refresh
        </button>
      </div>
      <JobList jobs={jobs} onOpen={onOpen} />
    </>
  );
}

function JobScreen({
  jobId,
  preloaded,
  onBack,
}: {
  jobId: string;
  preloaded: Job | undefined;
  onBack: () => void;
}) {
  const [job, setJob] = useState<Job | null>(preloaded ?? null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // A deep link can land here with no feed loaded at all, so the detail screen
    // always fetches its own job rather than depending on the list.
    if (preloaded) {
      setJob(preloaded);
      return;
    }
    const controller = new AbortController();
    let active = true;
    setError(null);
    fetchJob(jobId, controller.signal)
      .then((found) => active && setJob(found))
      .catch((cause: unknown) => {
        if (!active) return;
        if (cause instanceof ApiError && cause.status === 404) {
          setError("That job is no longer available.");
        } else {
          setError(cause instanceof Error ? cause.message : "Could not load that job.");
        }
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [jobId, preloaded]);

  if (error) {
    return (
      <ErrorState title="Could not open that job" onRetry={onBack}>
        <p>{error}</p>
      </ErrorState>
    );
  }
  if (!job) return <LoadingState label="Loading job…" />;
  return <JobDetail job={job} onBack={onBack} />;
}
