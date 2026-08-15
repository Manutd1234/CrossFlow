import { type KeyboardEvent, useRef } from 'react';
import { ChartColumn, Map, NavigationIcon } from 'lucide-react';
import { ICON_SIZE } from '../../theme/iconSizes';

export type AppTab = 'map' | 'route' | 'analytics';

interface NavigationProps {
  activeTab: AppTab;
  setActiveTab: (tab: AppTab) => void;
}

const NAV_ITEMS = [
  {
    id: 'map',
    label: 'Congestion',
    description: 'Network conditions',
    icon: Map,
  },
  {
    id: 'route',
    label: 'Route',
    description: 'Plan a road journey',
    icon: NavigationIcon,
  },
  {
    id: 'analytics',
    label: 'Analytics',
    description: 'Ferry, port & operations',
    icon: ChartColumn,
  },
] as const satisfies ReadonlyArray<{
  id: AppTab;
  label: string;
  description: string;
  icon: typeof Map;
}>;

export function Navigation({ activeTab, setActiveTab }: NavigationProps) {
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const handleTabKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    currentIndex: number,
  ) => {
    let nextIndex: number | undefined;

    switch (event.key) {
      case 'ArrowRight':
        nextIndex = (currentIndex + 1) % NAV_ITEMS.length;
        break;
      case 'ArrowLeft':
        nextIndex = (currentIndex - 1 + NAV_ITEMS.length) % NAV_ITEMS.length;
        break;
      case 'Home':
        nextIndex = 0;
        break;
      case 'End':
        nextIndex = NAV_ITEMS.length - 1;
        break;
      default:
        return;
    }

    event.preventDefault();
    const nextItem = NAV_ITEMS[nextIndex];
    setActiveTab(nextItem.id);
    tabRefs.current[nextIndex]?.focus();
  };

  return (
    <nav
      className="app-navigation app-navigation--top"
      aria-label="Primary workspace navigation"
    >
      <ul
        className="app-navigation-list app-navigation-tab-list"
        role="tablist"
        aria-label="Workspace views"
        aria-orientation="horizontal"
      >
        {NAV_ITEMS.map((item, index) => {
          const Icon = item.icon;
          const isActive = activeTab === item.id;

          return (
            <li key={item.id} role="presentation">
              <button
                ref={(element) => {
                  tabRefs.current[index] = element;
                }}
                type="button"
                id={'workspace-tab-' + item.id}
                className="app-navigation-button app-navigation-tab"
                role="tab"
                aria-selected={isActive}
                aria-current={isActive ? 'page' : undefined}
                aria-controls="main-content"
                tabIndex={isActive ? 0 : -1}
                onClick={() => setActiveTab(item.id)}
                onKeyDown={(event) => handleTabKeyDown(event, index)}
              >
                <span className="app-navigation-icon" aria-hidden="true">
                  <Icon size={ICON_SIZE.large} strokeWidth={2.1} />
                </span>
                <span className="app-navigation-copy">
                  <strong>{item.label}</strong>
                  <span>{item.description}</span>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
