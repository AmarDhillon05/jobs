/** Human-friendly "when", used in the feed. Returns "" for missing input. */
export function formatWhen(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "";
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "";

  const seconds = Math.round((now.getTime() - when.getTime()) / 1000);
  if (seconds < 0) return "just now";
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return when.toISOString().slice(0, 10);
}

/** Absolute timestamp for the detail screen. */
export function formatExact(iso: string | null | undefined): string {
  if (!iso) return "Unknown";
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "Unknown";
  return when.toISOString().replace("T", " ").slice(0, 16) + " UTC";
}
