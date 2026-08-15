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
