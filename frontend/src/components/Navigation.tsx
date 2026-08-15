import { type KeyboardEvent, useRef } from 'react';
import { ChartColumn, Route, TrafficCone } from 'lucide-react';

export type NavigationTab = 'congestion' | 'route' | 'analytics';

interface NavigationProps {
  activeTab: NavigationTab;
  onTabChange: (tab: NavigationTab) => void;
}

const NAVIGATION_ITEMS = [
  { id: 'congestion', label: 'Congestion', icon: TrafficCone },
  { id: 'route', label: 'Route', icon: Route },
  { id: 'analytics', label: 'Analytics', icon: ChartColumn },
] as const;

export function Navigation({ activeTab, onTabChange }: NavigationProps) {
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const handleKeyDown = (event: KeyboardEvent<HTMLButtonElement>, currentIndex: number) => {
    let nextIndex: number | undefined;

    if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % NAVIGATION_ITEMS.length;
    if (event.key === 'ArrowLeft') {
      nextIndex = (currentIndex - 1 + NAVIGATION_ITEMS.length) % NAVIGATION_ITEMS.length;
    }
    if (event.key === 'Home') nextIndex = 0;
    if (event.key === 'End') nextIndex = NAVIGATION_ITEMS.length - 1;
    if (nextIndex === undefined) return;

    event.preventDefault();
    const nextItem = NAVIGATION_ITEMS[nextIndex];
    onTabChange(nextItem.id);
    tabRefs.current[nextIndex]?.focus();
  };

  return (
    <nav className="application-navigation" aria-label="Primary navigation">
      <ul className="application-navigation__list" role="tablist">
        {NAVIGATION_ITEMS.map((item, index) => {
          const Icon = item.icon;
          const isActive = item.id === activeTab;

          return (
            <li key={item.id} role="presentation">
              <button
                ref={(element) => {
                  tabRefs.current[index] = element;
                }}
                type="button"
                className="application-navigation__tab"
                role="tab"
                aria-selected={isActive}
                aria-current={isActive ? 'page' : undefined}
                tabIndex={isActive ? 0 : -1}
                onClick={() => onTabChange(item.id)}
                onKeyDown={(event) => handleKeyDown(event, index)}
              >
                <span className="application-navigation__icon" aria-hidden="true">
                  <Icon size={20} strokeWidth={2.1} />
                </span>
                <span>{item.label}</span>
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
