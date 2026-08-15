import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { Header } from './Header';

describe('Header', () => {
  it.each([
    ['live', 'Live', 'status-badge--green'],
    ['model-estimate', 'Model Estimate', 'status-badge--orange'],
    ['local-estimate', 'Local Estimate', 'status-badge--orange'],
  ])('renders the brand and %s status without subtext', (status, label, statusClass) => {
    const markup = renderToStaticMarkup(createElement(Header, {
      status,
      activeTab: 'congestion',
      onTabChange: () => {},
    }));

    expect(markup).toContain('Cross');
    expect(markup).toContain('Flow');
    expect(markup).toContain(`>${label}</span>`);
    expect(markup).toContain(statusClass);
    expect(markup).not.toContain('Network conditions');
    expect(markup).not.toContain('Plan a road journey');
  });
});
