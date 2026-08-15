import { describe, expect, it } from 'vitest';

import { ICON_SIZE } from './iconSizes';

describe('ICON_SIZE', () => {
  it('keeps the shared Lucide sizes aligned with the UI scale', () => {
    expect(ICON_SIZE).toEqual({
      massive: 32,
      big: 20,
      large: 18,
      medium: 16,
      small: 12,
    });
  });
});
