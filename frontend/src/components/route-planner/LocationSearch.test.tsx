/* @vitest-environment jsdom */

import { act, type ReactElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import type { RouteLocation } from '../../types';
import { LocationSearch } from './LocationSearch';

let root: Root | undefined;

function renderIntoDom(element: ReactElement): HTMLElement {
  const container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
  act(() => root?.render(element));
  return container;
}

function press(input: HTMLInputElement, key: string): void {
  act(() => {
    input.dispatchEvent(new KeyboardEvent('keydown', {
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

describe('LocationSearch keyboard navigation', () => {
  it('reuses the location menu for read-only saved-place selection', () => {
    const onNamedLocationSelect = vi.fn();
    const namedLocations: RouteLocation[] = [
      {
        id: 'origin',
        name: 'Batamindo Industrial Park',
        category: 'Industry',
        lat: 1.0605,
        lng: 104.0303,
      },
      {
        id: 'destination',
        name: 'Batam Centre Ferry Terminal',
        category: 'Ferry terminal',
        lat: 1.1318,
        lng: 104.0554,
      },
    ];
    const container = renderIntoDom(
      <LocationSearch
        id="saved-origin"
        label="From"
        value={namedLocations[0]}
        onChange={vi.fn()}
        onNamedLocationSelect={onNamedLocationSelect}
        namedLocations={namedLocations}
        compactLayout
        savedPlacesOnly
      />,
    );

    const input = container.querySelector<HTMLInputElement>('#saved-origin');
    expect(input?.readOnly).toBe(true);
    expect(input?.closest('.location-search-compact')).not.toBeNull();

    act(() => input?.focus());
    const menu = container.querySelector('#saved-origin-options');
    const options = Array.from(menu?.querySelectorAll<HTMLButtonElement>('[role="option"]') ?? []);
    expect(menu?.textContent).toContain('SAVED PLACES');
    expect(menu?.textContent).not.toContain('Use my current location');
    expect(options).toHaveLength(2);

    act(() => options[1]?.click());
    expect(onNamedLocationSelect).toHaveBeenCalledWith(namedLocations[1]);
  });

  it('can hide visible action buttons while keeping the map picker suggestion', () => {
    const container = renderIntoDom(
      <LocationSearch
        id="sidebar-location"
        label="From"
        value={null}
        onChange={vi.fn()}
        namedLocations={[]}
        onOpenMapPicker={vi.fn()}
        showMapPickerButton={false}
        showSearchButton={false}
      />,
    );

    const visibleButtonLabels = Array.from(container.querySelectorAll('button'))
      .map(button => button.textContent?.trim());
    expect(visibleButtonLabels).not.toContain('Pick on map');
    expect(visibleButtonLabels).not.toContain('Search');

    const input = container.querySelector<HTMLInputElement>('#sidebar-location');
    act(() => input?.focus());
    expect(container.querySelector('#sidebar-location-options-map-picker')).not.toBeNull();
  });

  it('tracks an active option, selects it with Enter, and resets it with Escape', () => {
    const onChange = vi.fn();
    const onOpenMapPicker = vi.fn();
    const namedLocations: RouteLocation[] = [{
      id: 'batam-centre',
      name: 'Batam Centre Ferry Terminal',
      category: 'Ferry terminal',
      lat: 1.1318,
      lng: 104.0554,
    }];
    const container = renderIntoDom(
      <LocationSearch
        id="origin-location"
        label="From"
        value={null}
        onChange={onChange}
        namedLocations={namedLocations}
        onOpenMapPicker={onOpenMapPicker}
      />,
    );
    const input = container.querySelector<HTMLInputElement>('#origin-location');
    const searchButton = container.querySelector<HTMLButtonElement>('[aria-label="Search Singapore or Batam places"]');
    expect(input).not.toBeNull();
    expect(searchButton?.classList.contains('ui-sand-interactive')).toBe(true);

    act(() => input?.focus());
    press(input!, 'ArrowUp');
    const wrappedOptionId = input?.getAttribute('aria-activedescendant');
    expect(wrappedOptionId).toBeTruthy();
    expect(document.getElementById(wrappedOptionId!)?.getAttribute('aria-selected')).toBe('true');

    press(input!, 'Escape');
    expect(input?.getAttribute('aria-expanded')).toBe('false');
    expect(input?.hasAttribute('aria-activedescendant')).toBe(false);

    press(input!, 'ArrowDown');
    expect(input?.getAttribute('aria-activedescendant'))
      .toBe('origin-location-options-map-picker');
    press(input!, 'ArrowDown');
    expect(input?.getAttribute('aria-activedescendant'))
      .toBe('origin-location-options-current-location');
    press(input!, 'ArrowDown');
    expect(input?.getAttribute('aria-activedescendant'))
      .toBe('origin-location-options-named-0');

    press(input!, 'Enter');
    expect(onChange).toHaveBeenCalledWith({
      lat: 1.1318,
      lng: 104.0554,
      display_name: 'Batam Centre Ferry Terminal',
    });
    expect(onOpenMapPicker).not.toHaveBeenCalled();
    expect(input?.getAttribute('aria-expanded')).toBe('false');
    expect(input?.hasAttribute('aria-activedescendant')).toBe(false);
  });
});
