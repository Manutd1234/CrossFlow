import {
  type KeyboardEvent,
  type ReactNode,
  useId,
  useRef,
} from 'react';
import './WorkspaceSubtabs.css';

export interface WorkspaceSubtab<TabId extends string = string> {
  id: TabId;
  label: string;
  description?: string;
  content: ReactNode;
}

export interface WorkspaceSubtabsProps<TabId extends string> {
  tabs: readonly WorkspaceSubtab<TabId>[];
  activeTab: TabId;
  onActiveTabChange: (tab: TabId) => void;
  ariaLabel: string;
  className?: string;
  idPrefix?: string;
  railAccessory?: ReactNode;
  unwrapped?: boolean;
}

export function WorkspaceSubtabs<TabId extends string>({
  tabs,
  activeTab,
  onActiveTabChange,
  ariaLabel,
  className,
  idPrefix,
  railAccessory,
  unwrapped = false,
}: WorkspaceSubtabsProps<TabId>) {
  const generatedId = useId();
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const baseId = idPrefix ?? `workspace-subtabs-${generatedId.replace(/:/g, '')}`;
  const requestedIndex = tabs.findIndex(tab => tab.id === activeTab);
  const activeIndex = requestedIndex >= 0 ? requestedIndex : 0;

  const activateTab = (index: number) => {
    const tab = tabs[index];
    if (!tab) return;

    onActiveTabChange(tab.id);
    tabRefs.current[index]?.focus();
  };

  const handleKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    currentIndex: number,
  ) => {
    let nextIndex: number | undefined;

    switch (event.key) {
      case 'ArrowRight':
        nextIndex = (currentIndex + 1) % tabs.length;
        break;
      case 'ArrowLeft':
        nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
        break;
      case 'Home':
        nextIndex = 0;
        break;
      case 'End':
        nextIndex = tabs.length - 1;
        break;
      default:
        return;
    }

    event.preventDefault();
    activateTab(nextIndex);
  };

  if (tabs.length === 0) return null;

  const content = (
    <>
      <div className="workspace-subtabs__rail">
        <div className="workspace-subtabs__scroll-region">
          <div
            className="workspace-subtabs__tablist"
            role="tablist"
            aria-label={ariaLabel}
            aria-orientation="horizontal"
          >
            {tabs.map((tab, index) => {
              const isActive = index === activeIndex;
              const tabId = `${baseId}-tab-${index}`;
              const panelId = `${baseId}-panel-${index}`;

              return (
                <button
                  key={tab.id}
                  ref={(element) => {
                    tabRefs.current[index] = element;
                  }}
                  type="button"
                  id={tabId}
                  className="workspace-subtabs__tab"
                  role="tab"
                  aria-selected={isActive}
                  aria-controls={panelId}
                  tabIndex={isActive ? 0 : -1}
                  onClick={() => activateTab(index)}
                  onKeyDown={event => handleKeyDown(event, index)}
                >
                  <span className="workspace-subtabs__label">{tab.label}</span>
                  {tab.description ? (
                    <span className="workspace-subtabs__description">
                      {tab.description}
                    </span>
                  ) : null}
                </button>
              );
            })}
          </div>
        </div>
        {railAccessory ? (
          <div className="workspace-subtabs__rail-accessory">
            {railAccessory}
          </div>
        ) : null}
      </div>

      {tabs.map((tab, index) => {
        const isActive = index === activeIndex;

        return (
          <div
            key={tab.id}
            id={`${baseId}-panel-${index}`}
            className="workspace-subtabs__panel"
            role="tabpanel"
            aria-labelledby={`${baseId}-tab-${index}`}
            tabIndex={isActive ? 0 : -1}
            hidden={!isActive}
          >
            {tab.content}
          </div>
        );
      })}
    </>
  );

  if (unwrapped) return content;

  return (
    <section
      className={['workspace-subtabs', className].filter(Boolean).join(' ')}
      aria-label={ariaLabel}
    >
      {content}
    </section>
  );
}
