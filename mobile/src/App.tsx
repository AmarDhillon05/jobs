import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { SafeAreaView, StyleSheet, Text, View } from "react-native";
import { StatusBar } from "expo-status-bar";
import * as Linking from "expo-linking";
import * as Notifications from "expo-notifications";

import { ApiError, fetchJob, fetchRecentJobs } from "./api";
import { JobDetail } from "./components/JobDetail";
import { JobList } from "./components/JobList";
import { EmptyState, ErrorState, LoadingState } from "./components/States";
import {
  configureForegroundBehaviour,
  registerForPushNotifications,
  type RegistrationResult,
} from "./notifications";
import { jobIdFromPath, parseNotificationPayload, routeForNotification, type Route } from "./routes";
import { theme } from "./theme";
import type { Job } from "./types";
import { mergeJobs } from "./validate";

export interface AppProps {
  /** Starting route. Tests set this; the app derives it from a cold-start link. */
  initialRoute?: Route;
  /** Injected in tests so push registration is not attempted. */
  registerPush?: () => Promise<RegistrationResult>;
  /** Injected in tests. */
  openUrl?: (url: string) => Promise<unknown>;
}

/**
 * The whole app: a feed of recently discovered internships, a detail screen, and
 * the notification entry point.
 *
 * Routing is a two-state machine rather than a navigation library. The only route
 * that has to work is the one a push notification produces, and keeping it
 * explicit makes that path directly testable - which matters more here than
 * having a stack animation.
 */
export default function App({ initialRoute, registerPush, openUrl }: AppProps = {}) {
  const [route, setRoute] = useState<Route>(initialRoute ?? { name: "feed" });
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [banner, setBanner] = useState<string | null>(null);
  const [pushState, setPushState] = useState<RegistrationResult | null>(null);
  const handledColdStart = useRef(false);

  const go = useCallback((next: Route) => setRoute(next), []);

  // ------------------------------------------------------------------ the feed
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setListError(null);
    fetchRecentJobs({ signal: controller.signal })
      .then((response) => {
        if (!active) return;
        // Merge rather than replace: a job already on screen (one that arrived via
        // a push while the feed was open) must not appear twice.
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

  // -------------------------------------------------------------- notifications
  useEffect(() => {
    configureForegroundBehaviour();
    const register = registerPush ?? registerForPushNotifications;
    let active = true;
    register().then((result) => {
      if (active) setPushState(result);
    });
    return () => {
      active = false;
    };
  }, [registerPush]);

  const handleNotification = useCallback(
    (raw: unknown, source: string) => {
      const payload = parseNotificationPayload(raw);
      const target = routeForNotification(payload);
      go(target);
      setBanner(
        target.name === "job"
          ? `Opened from ${source}`
          : `New internships found — showing the latest (${source})`,
      );
      // A notification means the backend knows something we do not.
      setReloadToken((token) => token + 1);
    },
    [go],
  );

  useEffect(() => {
    // Tapped while the app was running or backgrounded.
    const tapped = Notifications.addNotificationResponseReceivedListener((response) => {
      handleNotification(response?.notification?.request?.content?.data, "notification");
    });
    // Arrived while the app was foregrounded: refresh the feed, do not yank the
    // user off whatever they are reading.
    const received = Notifications.addNotificationReceivedListener(() => {
      setReloadToken((token) => token + 1);
      setBanner("New internship found");
    });

    // Cold start: the app was launched *by* the tap, so the response is waiting.
    if (!handledColdStart.current) {
      handledColdStart.current = true;
      void Notifications.getLastNotificationResponseAsync().then((response) => {
        if (response) {
          handleNotification(response.notification?.request?.content?.data, "notification");
        }
      });
    }

    return () => {
      tapped.remove();
      received.remove();
    };
  }, [handleNotification]);

  // Cold start via a URL deep link (internshipmonitor://jobs/<id>, or the web link).
  useEffect(() => {
    if (initialRoute) return;
    let active = true;
    void Linking.getInitialURL().then((url) => {
      if (!active || !url) return;
      const jobId = jobIdFromPath(url);
      if (jobId) {
        go({ name: "job", jobId });
        setBanner("Opened from a link");
      }
    });
    const subscription = Linking.addEventListener("url", ({ url }) => {
      const jobId = jobIdFromPath(url);
      if (jobId) go({ name: "job", jobId });
    });
    return () => {
      active = false;
      subscription.remove();
    };
  }, [go, initialRoute]);

  const selected = useMemo(
    () => (route.name === "job" ? jobs?.find((job) => job.job_id === route.jobId) : undefined),
    [route, jobs],
  );

  return (
    <SafeAreaView style={styles.screen}>
      <StatusBar style="light" />
      <View style={styles.container}>
        <View style={styles.masthead}>
          <Text style={styles.heading}>Internship Monitor</Text>
          {jobs ? <Text style={styles.count}>{jobs.length} recent</Text> : null}
        </View>

        {banner ? (
          <View style={styles.banner} testID="banner">
            <Text style={styles.bannerText}>{banner}</Text>
          </View>
        ) : null}

        {pushState && pushState.status !== "registered" ? (
          <View style={styles.notice} testID="push-notice">
            <Text style={styles.noticeText}>
              Notifications are off: {pushState.detail ?? pushState.status}
            </Text>
          </View>
        ) : null}

        {route.name === "job" ? (
          <JobScreen
            jobId={route.jobId}
            preloaded={selected}
            onBack={() => go({ name: "feed" })}
            openUrl={openUrl}
          />
        ) : (
          <FeedScreen
            jobs={jobs}
            error={listError}
            onOpen={(jobId) => go({ name: "job", jobId })}
            onRefresh={() => setReloadToken((token) => token + 1)}
          />
        )}
      </View>
    </SafeAreaView>
  );
}

function FeedScreen({
  jobs,
  error,
  onOpen,
  onRefresh,
}: {
  jobs: Job[] | null;
  error: string | null;
  onOpen: (jobId: string) => void;
  onRefresh: () => void;
}) {
  if (error) {
    return (
      <ErrorState
        title="Could not load jobs"
        message={error}
        actionLabel="Try again"
        onAction={onRefresh}
      />
    );
  }
  if (jobs === null) return <LoadingState />;
  if (jobs.length === 0) {
    return (
      <EmptyState
        title="No internships yet"
        message="Nothing has been discovered in the last poll. New roles appear here within about ten minutes of being posted."
        actionLabel="Refresh"
        onAction={onRefresh}
      />
    );
  }
  return <JobList jobs={jobs} onOpen={onOpen} onRefresh={onRefresh} />;
}

function JobScreen({
  jobId,
  preloaded,
  onBack,
  openUrl,
}: {
  jobId: string;
  preloaded: Job | undefined;
  onBack: () => void;
  openUrl?: (url: string) => Promise<unknown>;
}) {
  const [job, setJob] = useState<Job | null>(preloaded ?? null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // A notification tap can land here with no feed loaded at all, so the detail
    // screen always fetches its own job rather than depending on the list.
    if (preloaded) {
      setJob(preloaded);
      return;
    }
    const controller = new AbortController();
    let active = true;
    setError(null);
    fetchJob(jobId, controller.signal)
      .then((found) => {
        if (active) setJob(found);
      })
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
      <ErrorState
        title="Could not open that job"
        message={error}
        actionLabel="Back to recent jobs"
        onAction={onBack}
      />
    );
  }
  if (!job) return <LoadingState label="Loading job…" />;
  return <JobDetail job={job} onBack={onBack} openUrl={openUrl} />;
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: theme.bg },
  container: { flex: 1, paddingHorizontal: 16, paddingTop: 8 },
  masthead: { flexDirection: "row", alignItems: "baseline", gap: 12, marginBottom: 16 },
  heading: { color: theme.text, fontSize: 20, fontWeight: "700" },
  count: { color: theme.muted, fontSize: 13 },
  banner: {
    borderWidth: 1,
    borderColor: theme.accent,
    backgroundColor: "rgba(91,140,255,0.12)",
    borderRadius: theme.radius,
    padding: 10,
    marginBottom: 12,
  },
  bannerText: { color: theme.text, fontSize: 14 },
  notice: {
    borderWidth: 1,
    borderColor: theme.border,
    borderRadius: theme.radius,
    padding: 10,
    marginBottom: 12,
  },
  noticeText: { color: theme.muted, fontSize: 13 },
});
