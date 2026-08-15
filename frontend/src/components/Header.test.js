import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { Header } from './Header';

describe('Header', () => {
  it('renders the brand and requested status without a presentation button', () => {
    const markup = renderToStaticMarkup(createElement(Header, {
      statusTone: 'gray',
      statusLabel: 'Local continuity',
      statusDetail: 'Waiting for sync',
    }));

    expect(markup).toContain('Cross');
    expect(markup).toContain('Flow');
    expect(markup).toContain('Local continuity');
    expect(markup).toContain('Waiting for sync');
    expect(markup).not.toContain('<button');
  });
});
