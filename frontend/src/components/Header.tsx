import { Badge } from './Badge';
import { Navigation, type NavigationTab } from './Navigation';

export type ApplicationStatus = 'live' | 'model-estimate' | 'local-estimate';

interface HeaderProps {
  status: ApplicationStatus;
  activeTab: NavigationTab;
  onTabChange: (tab: NavigationTab) => void;
}

const APPLICATION_STATUS = {
  live: { label: 'Live', tone: 'green' },
  'model-estimate': { label: 'Model Estimate', tone: 'orange' },
  'local-estimate': { label: 'Local Estimate', tone: 'orange' },
} as const;

export function Header({ status, activeTab, onTabChange }: HeaderProps) {
  const statusPresentation = APPLICATION_STATUS[status];

  return (
    <header className="application-header">
      <div className="application-header__content">
        <div className="application-brand">
          <h1 className="application-brand__name">
            Cross<span>Flow</span>
          </h1>
        </div>

        <Navigation activeTab={activeTab} onTabChange={onTabChange} />

        <Badge
          tone={statusPresentation.tone}
          label={statusPresentation.label}
          role="status"
        />
      </div>
    </header>
  );
}
