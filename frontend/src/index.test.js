import { readFileSync } from 'node:fs';
import { URL } from 'node:url';
import { describe, expect, it } from 'vitest';

const stylesheet = readFileSync(new URL('./index.css', import.meta.url), 'utf8');

describe('global typography', () => {
  it('defines text typography only for body and the supported headings', () => {
    expect(stylesheet).toMatch(/body\s*{/);
    expect(stylesheet).toMatch(/h1,\s*\n+h2\s*{/);
    expect(stylesheet).toMatch(/h1\s*{/);
    expect(stylesheet).toMatch(/h2\s*{/);
    expect(stylesheet).not.toMatch(/h[3-6]\s*[,{]/);
  });
});

describe('status palette', () => {
  it.each(['red', 'orange', 'green', 'blue', 'gray'])('defines %s status colors', (tone) => {
    expect(stylesheet).toContain(`--status-${tone}-background:`);
    expect(stylesheet).toContain(`--status-${tone}-border:`);
    expect(stylesheet).toContain(`--status-${tone}-text:`);
  });

  it('does not restore the reference border or sunset tokens', () => {
    expect(stylesheet).not.toContain('--border-color');
    expect(stylesheet).not.toContain('--sunset');
  });
});
