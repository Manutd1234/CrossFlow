import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { Badge } from './Badge';

describe('Badge', () => {
  it.each(['red', 'orange', 'green', 'blue', 'gray'])('uses the shared %s status style', (tone) => {
    const markup = renderToStaticMarkup(createElement(Badge, {
      tone,
      label: 'Status',
    }));

    expect(markup).toContain('status-badge');
    expect(markup).toContain(`status-badge--${tone}`);
  });
});
