/* @vitest-environment jsdom */

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { PitchDeckModal } from './PitchDeckModal';

let root: Root | undefined;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = undefined;
  document.body.replaceChildren();
});

describe('stage presentation fit', () => {
  it('keeps both slides in a non-scrolling canvas with visible title text', () => {
    const container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
    act(() => root?.render(<PitchDeckModal isOpen onClose={vi.fn()} />));

    const dialog = container.querySelector<HTMLElement>('[role="dialog"]');
    let canvas = container.querySelector<HTMLElement>('[role="document"]');
    let title = canvas?.querySelector<HTMLElement>('h3');
    expect(dialog?.style.overflow).toBe('hidden');
    expect(canvas?.style.overflow).toBe('hidden');
    expect(canvas?.style.minHeight).toBe('0px');
    expect(title?.style.color).toBe('var(--text-primary)');
    expect(title?.style.webkitTextFillColor).toBe('');

    const next = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Go to next presentation slide"]',
    );
    act(() => next?.click());
    canvas = container.querySelector<HTMLElement>('[role="document"]');
    title = canvas?.querySelector<HTMLElement>('h3');
    expect(canvas?.getAttribute('aria-label')).toBe('Slide 2 of 2');
    expect(canvas?.textContent).toContain('Core Technical Capabilities');
    expect(canvas?.style.overflow).toBe('hidden');
    expect(title?.style.color).toBe('var(--text-primary)');
    expect(title?.style.webkitTextFillColor).toBe('');
  });
});
