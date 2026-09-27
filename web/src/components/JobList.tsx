import type { Job } from "../types";
import { formatWhen } from "../format";

interface JobListProps {
  jobs: Job[];
  onOpen: (jobId: string) => void;
}

export function JobList({ jobs, onOpen }: JobListProps) {
  return (
    <ul className="job-list" data-testid="job-list">
      {jobs.map((job) => (
        <li key={job.job_id}>
          <button
            className="job-card"
            onClick={() => onOpen(job.job_id)}
            data-testid="job-card"
            data-job-id={job.job_id}
          >
            <div className="company">{job.company}</div>
            <div className="title">{job.title}</div>
            <div className="meta">
              <span>{job.location ?? "Location not specified"}</span>
              <span>{formatWhen(job.first_seen)}</span>
              <span className="score" aria-label={`Relevance ${job.relevance_score} of 100`}>
                {job.relevance_score}
              </span>
            </div>
          </button>
        </li>
      ))}
    </ul>
  );
}
