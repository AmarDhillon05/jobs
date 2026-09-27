import { FlatList, Pressable, StyleSheet, Text, View } from "react-native";

import { formatWhen } from "../format";
import { theme } from "../theme";
import type { Job } from "../types";

interface JobListProps {
  jobs: Job[];
  onOpen: (jobId: string) => void;
  onRefresh?: () => void;
  refreshing?: boolean;
}

export function JobList({ jobs, onOpen, onRefresh, refreshing = false }: JobListProps) {
  return (
    <FlatList
      testID="job-list"
      data={jobs}
      keyExtractor={(job) => job.job_id}
      onRefresh={onRefresh}
      refreshing={refreshing}
      contentContainerStyle={styles.list}
      renderItem={({ item }) => (
        <Pressable
          style={styles.card}
          onPress={() => onOpen(item.job_id)}
          testID={`job-card-${item.job_id}`}
          accessibilityRole="button"
          accessibilityLabel={`${item.company}, ${item.title}`}
        >
          <Text style={styles.company}>{item.company}</Text>
          <Text style={styles.title}>{item.title}</Text>
          <View style={styles.meta}>
            <Text style={styles.metaText}>{item.location ?? "Location not specified"}</Text>
            <Text style={styles.metaText}>{formatWhen(item.first_seen)}</Text>
            <Text style={styles.score}>{item.relevance_score}</Text>
          </View>
        </Pressable>
      )}
    />
  );
}

const styles = StyleSheet.create({
  list: { gap: 10, paddingBottom: 40 },
  card: {
    backgroundColor: theme.surface,
    borderWidth: 1,
    borderColor: theme.border,
    borderRadius: theme.radius,
    padding: 14,
  },
  company: { color: theme.text, fontWeight: "700" },
  title: { color: theme.text, marginTop: 2 },
  meta: { flexDirection: "row", gap: 10, marginTop: 6, flexWrap: "wrap" },
  metaText: { color: theme.muted, fontSize: 13 },
  score: { color: theme.ok, fontSize: 13, fontVariant: ["tabular-nums"] },
});
