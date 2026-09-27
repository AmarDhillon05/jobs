import { Linking, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import { formatExact } from "../format";
import { theme } from "../theme";
import type { Job } from "../types";

interface JobDetailProps {
  job: Job;
  onBack: () => void;
  /** Injected in tests; defaults to the OS handler. */
  openUrl?: (url: string) => Promise<unknown>;
}

export function JobDetail({ job, onBack, openUrl = Linking.openURL }: JobDetailProps) {
  return (
    <ScrollView testID={`job-detail-${job.job_id}`} contentContainerStyle={styles.body}>
      <Pressable onPress={onBack} testID="back" accessibilityRole="button">
        <Text style={styles.back}>← Recent jobs</Text>
      </Pressable>

      <Text style={styles.title}>{job.title}</Text>
      <Text style={styles.company}>{job.company}</Text>

      <View style={styles.rows}>
        <Row label="Location" value={job.location ?? "Not specified"} testID="detail-location" />
        <Row label="Posted" value={formatExact(job.date_posted)} />
        <Row label="First seen" value={formatExact(job.first_seen)} testID="detail-first-seen" />
        <Row label="Relevance" value={`${job.relevance_score}/100`} />
        {job.employment_type ? <Row label="Type" value={job.employment_type} /> : null}
        <Row label="Source" value={job.source} />
      </View>

      {job.description ? (
        <Text style={styles.description} testID="detail-description">
          {job.description}
        </Text>
      ) : null}

      <Pressable
        style={styles.apply}
        testID="apply-button"
        accessibilityRole="button"
        onPress={() => {
          // Opens the original careers/ATS page in the system browser. Deliberately
          // not an in-app webview: applications involve credentials, and the user
          // should see the real address bar.
          void openUrl(job.url);
        }}
      >
        <Text style={styles.applyText}>Open Application</Text>
      </Pressable>
      <Text style={styles.url} testID="apply-url">
        {job.url}
      </Text>
    </ScrollView>
  );
}

function Row({ label, value, testID }: { label: string; value: string; testID?: string }) {
  return (
    <View style={styles.row}>
      <Text style={styles.rowLabel}>{label}</Text>
      <Text style={styles.rowValue} testID={testID}>
        {value}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  body: { paddingBottom: 48, gap: 4 },
  back: { color: theme.accent, marginBottom: 12 },
  title: { color: theme.text, fontSize: 19, fontWeight: "700" },
  company: { color: theme.muted, marginBottom: 12 },
  rows: { gap: 6, marginVertical: 12 },
  row: { flexDirection: "row", gap: 12 },
  rowLabel: { color: theme.muted, width: 96 },
  rowValue: { color: theme.text, flex: 1 },
  description: { color: "#cfd6ea", lineHeight: 21, marginVertical: 12 },
  apply: {
    backgroundColor: theme.accent,
    borderRadius: 10,
    paddingVertical: 14,
    alignItems: "center",
    marginTop: 12,
  },
  applyText: { color: theme.accentText, fontWeight: "700", fontSize: 16 },
  url: { color: theme.muted, fontSize: 12, marginTop: 8 },
});
