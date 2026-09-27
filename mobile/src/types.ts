/** The job shape the API serves (`JobRecord.to_api_dict` on the backend). */
export interface Job {
  job_id: string;
  company: string;
  title: string;
  location: string | null;
  url: string;
  source: string;
  date_posted: string | null;
  first_seen: string | null;
  last_seen: string | null;
  relevance_score: number;
  priority: string;
  industry: string;
  employment_type: string | null;
  description: string | null;
  notification_sent: boolean;
}

export interface JobListResponse {
  count: number;
  limit: number;
  jobs: Job[];
}

/** The `data` map a push notification carries (backend `format_push`). */
export interface NotificationPayload {
  schema_version?: string;
  urgency?: string;
  deep_link?: string;
  job_id?: string;
  job_ids?: string;
  job_count?: string;
  apply_url?: string;
}

export type LoadState<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error"; message: string };
