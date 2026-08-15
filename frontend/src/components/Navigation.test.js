import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { Navigation } from './Navigation';

describe('Navigation', () => {
  it('renders the three concise application tabs', () => {
    const markup = renderToStaticMarkup(createElement(Navigation, {
      activeTab: 'congestion',
      onTabChange: () => {},
    }));

    expect(markup).toContain('Congestion');
    expect(markup).toContain('Route');
    expect(markup).toContain('Analytics');
    expect(markup.match(/role="tab"/g)).toHaveLength(3);
    expect(markup).not.toContain('Network conditions');
    expect(markup).not.toContain('Plan a road journey');
    expect(markup).not.toContain('Flow &amp; carbon insights');
  });
});
