export type StatusTone = 'red' | 'orange' | 'green' | 'blue' | 'gray';

interface HeaderProps {
  statusTone: StatusTone;
  statusLabel: string;
  statusDetail: string;
}

export function Header({ statusTone, statusLabel, statusDetail }: HeaderProps) {
  return (
    <header className="application-header">
      <div className="application-header__content">
        <div className="application-brand">
          <h1 className="application-brand__name">
            Cross<span>Flow</span>
          </h1>
        </div>

        <div
          className={`system-status-card system-status-card--${statusTone}`}
          role="status"
          aria-label={`${statusLabel}. ${statusDetail}`}
        >
          <span className="system-status-card__indicator" aria-hidden="true" />
          <span className="system-status-card__content">
            <strong>{statusLabel}</strong>
            <span>{statusDetail}</span>
          </span>
        </div>
      </div>
    </header>
  );
}
