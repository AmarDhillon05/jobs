import type { Job } from "../types";
import { formatExact } from "../format";

interface JobDetailProps {
  job: Job;
  onBack: () => void;
}

export function JobDetail({ job, onBack }: JobDetailProps) {
  return (
    <section className="detail" data-testid="job-detail" data-job-id={job.job_id}>
      <button onClick={onBack} data-testid="back">
        ← Recent jobs
      </button>
      <h2>{job.title}</h2>
      <div className="company">{job.company}</div>

      <dl>
        <dt>Location</dt>
        <dd data-testid="detail-location">{job.location ?? "Not specified"}</dd>
        <dt>Posted</dt>
        <dd>{formatExact(job.date_posted)}</dd>
        <dt>First seen</dt>
        <dd data-testid="detail-first-seen">{formatExact(job.first_seen)}</dd>
        <dt>Relevance</dt>
        <dd>{job.relevance_score}/100</dd>
        {job.employment_type && (
          <>
            <dt>Type</dt>
            <dd>{job.employment_type}</dd>
          </>
        )}
        <dt>Source</dt>
        <dd>{job.source}</dd>
      </dl>

      {job.description && (
        <p className="description" data-testid="detail-description">
          {job.description}
        </p>
      )}

      {/* rel=noreferrer as well as noopener: the target is a third-party ATS. */}
      <a
        className="apply"
        href={job.url}
        target="_blank"
        rel="noopener noreferrer"
        data-testid="apply-link"
      >
        Open Application
      </a>
    </section>
  );
}
