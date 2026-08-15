export type BadgeTone = 'red' | 'orange' | 'green' | 'blue' | 'gray';

interface BadgeProps {
  label: string;
  tone: BadgeTone;
  detail?: string;
  role?: 'status';
}

export function Badge({ label, tone, detail, role }: BadgeProps) {
  const accessibleLabel = detail ? `${label}. ${detail}` : label;

  return (
    <span
      className={`status-badge status-badge--${tone}`}
      role={role}
      aria-label={accessibleLabel}
      title={detail}
    >
      {label}
    </span>
  );
}
