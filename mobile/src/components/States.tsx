import { ActivityIndicator, Pressable, StyleSheet, Text, View } from "react-native";

import { theme } from "../theme";

interface StateProps {
  title: string;
  message?: string;
  actionLabel?: string;
  onAction?: () => void;
}

export function EmptyState({ title, message, actionLabel, onAction }: StateProps) {
  return (
    <View style={styles.box} accessibilityRole="summary" testID="empty-state">
      <Text style={styles.title}>{title}</Text>
      {message ? <Text style={styles.message}>{message}</Text> : null}
      {onAction && actionLabel ? (
        <Pressable style={styles.button} onPress={onAction} testID="empty-action">
          <Text style={styles.buttonText}>{actionLabel}</Text>
        </Pressable>
      ) : null}
    </View>
  );
}

export function ErrorState({ title, message, actionLabel, onAction }: StateProps) {
  return (
    <View style={[styles.box, styles.error]} accessibilityRole="alert" testID="error-state">
      <Text style={[styles.title, { color: theme.danger }]}>{title}</Text>
      {message ? <Text style={styles.message}>{message}</Text> : null}
      {onAction && actionLabel ? (
        <Pressable style={styles.button} onPress={onAction} testID="error-action">
          <Text style={styles.buttonText}>{actionLabel}</Text>
        </Pressable>
      ) : null}
    </View>
  );
}

export function LoadingState({ label = "Loading jobs…" }: { label?: string }) {
  return (
    <View style={styles.box} testID="loading-state">
      <ActivityIndicator color={theme.accent} />
      <Text style={styles.message}>{label}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  box: {
    borderWidth: 1,
    borderColor: theme.border,
    borderStyle: "dashed",
    borderRadius: theme.radius,
    padding: 28,
    alignItems: "center",
    gap: 8,
  },
  error: { borderColor: theme.danger, borderStyle: "solid" },
  title: { color: theme.text, fontSize: 16, fontWeight: "600" },
  message: { color: theme.muted, textAlign: "center", lineHeight: 20 },
  button: {
    marginTop: 8,
    backgroundColor: theme.surface,
    borderWidth: 1,
    borderColor: theme.border,
    borderRadius: 10,
    paddingVertical: 10,
    paddingHorizontal: 18,
  },
  buttonText: { color: theme.text, fontWeight: "600" },
});
