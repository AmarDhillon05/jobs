interface StateProps {
  title: string;
  children?: React.ReactNode;
  onRetry?: () => void;
}

export function EmptyState({ title, children }: StateProps) {
  return (
    <div className="state" role="status" data-testid="empty-state">
      <h2>{title}</h2>
      {children}
    </div>
  );
}

export function ErrorState({ title, children, onRetry }: StateProps) {
  return (
    <div className="state error" role="alert" data-testid="error-state">
      <h2>{title}</h2>
      {children}
      {onRetry && (
        <p>
          <button onClick={onRetry}>Try again</button>
        </p>
      )}
    </div>
  );
}

export function LoadingState({ label = "Loading jobs…" }: { label?: string }) {
  return (
    <div className="state" role="status" aria-live="polite" data-testid="loading-state">
      {label}
    </div>
  );
}
