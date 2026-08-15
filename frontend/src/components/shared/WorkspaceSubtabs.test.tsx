/* @vitest-environment jsdom */

import { act, type ReactElement, useState } from 'react';
import { afterEach, describe, expect, it } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import {
  WorkspaceSubtabs,
  type WorkspaceSubtab,
} from './WorkspaceSubtabs';

type DemoTab = 'overview' | 'conditions' | 'sources';

const DEMO_TABS: readonly WorkspaceSubtab<DemoTab>[] = [
  {
    id: 'overview',
    label: 'Overview',
    description: 'At a glance',
    content: <p>Overview panel</p>,
  },
  {
    id: 'conditions',
    label: 'Conditions',
    content: <p>Conditions panel</p>,
  },
  {
    id: 'sources',
    label: 'Sources',
    content: <p>Sources panel</p>,
  },
];

let root: Root | undefined;

function DemoSubtabs({
  railAccessory,
  unwrapped = false,
}: {
  railAccessory?: ReactElement;
  unwrapped?: boolean;
}) {
  const [activeTab, setActiveTab] = useState<DemoTab>('overview');

  return (
    <WorkspaceSubtabs
      idPrefix="demo"
      ariaLabel="Map sections"
      tabs={DEMO_TABS}
      activeTab={activeTab}
      onActiveTabChange={setActiveTab}
      railAccessory={railAccessory}
      unwrapped={unwrapped}
    />
  );
}

function renderIntoDom(element: ReactElement): HTMLElement {
  const container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
  act(() => root?.render(element));
  return container;
}

function press(button: HTMLButtonElement, key: string): void {
  act(() => {
    button.dispatchEvent(new KeyboardEvent('keydown', {
      key,
      bubbles: true,
      cancelable: true,
    }));
  });
}

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = undefined;
  document.body.replaceChildren();
});

describe('WorkspaceSubtabs', () => {
  it('renders workspace-wide status at the end of the tab rail', () => {
    const container = renderIntoDom(
      <DemoSubtabs railAccessory={<span>API connected</span>} />,
    );
    const rail = container.querySelector('.workspace-subtabs__rail');
    const accessory = container.querySelector('.workspace-subtabs__rail-accessory');

    expect(accessory?.textContent).toBe('API connected');
    expect(accessory?.parentElement).toBe(rail);
    expect(rail?.lastElementChild).toBe(accessory);
  });

  it('can expose the rail and panels as direct parent-grid items', () => {
    const container = renderIntoDom(<DemoSubtabs unwrapped />);
    const rail = container.querySelector('.workspace-subtabs__rail');
    const panels = Array.from(container.querySelectorAll('[role="tabpanel"]'));

    expect(container.querySelector('.workspace-subtabs')).toBeNull();
    expect(rail?.parentElement).toBe(container);
    expect(panels).toHaveLength(3);
    expect(panels.every(panel => panel.parentElement === container)).toBe(true);
  });

  it('connects each tab to its panel and exposes only the active panel', () => {
    const container = renderIntoDom(<DemoSubtabs />);
    const tablist = container.querySelector('[role="tablist"]');
    const tabs = Array.from(
      container.querySelectorAll<HTMLButtonElement>('[role="tab"]'),
    );
    const panels = Array.from(
      container.querySelectorAll<HTMLElement>('[role="tabpanel"]'),
    );

    expect(tablist?.getAttribute('aria-label')).toBe('Map sections');
    expect(tabs).toHaveLength(3);
    expect(panels).toHaveLength(3);
    expect(tabs[0].getAttribute('aria-selected')).toBe('true');
    expect(tabs[0].tabIndex).toBe(0);
    expect(tabs[1].tabIndex).toBe(-1);
    expect(tabs[0].getAttribute('aria-controls')).toBe(panels[0].id);
    expect(panels[0].getAttribute('aria-labelledby')).toBe(tabs[0].id);
    expect(panels[0].hidden).toBe(false);
    expect(panels[1].hidden).toBe(true);

    act(() => tabs[1].click());

    expect(tabs[0].getAttribute('aria-selected')).toBe('false');
    expect(tabs[1].getAttribute('aria-selected')).toBe('true');
    expect(tabs[1].tabIndex).toBe(0);
    expect(panels[0].hidden).toBe(true);
    expect(panels[1].hidden).toBe(false);
    expect(document.activeElement).toBe(tabs[1]);
  });

  it('supports wrapped arrow navigation plus Home and End', () => {
    const container = renderIntoDom(<DemoSubtabs />);
    const tabs = Array.from(
      container.querySelectorAll<HTMLButtonElement>('[role="tab"]'),
    );

    act(() => tabs[0].focus());
    press(tabs[0], 'ArrowLeft');
    expect(document.activeElement).toBe(tabs[2]);
    expect(tabs[2].getAttribute('aria-selected')).toBe('true');

    press(tabs[2], 'ArrowRight');
    expect(document.activeElement).toBe(tabs[0]);
    expect(tabs[0].getAttribute('aria-selected')).toBe('true');

    press(tabs[0], 'End');
    expect(document.activeElement).toBe(tabs[2]);

    press(tabs[2], 'Home');
    expect(document.activeElement).toBe(tabs[0]);
    expect(tabs.filter(tab => tab.tabIndex === 0)).toHaveLength(1);
  });
});
