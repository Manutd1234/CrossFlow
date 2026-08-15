/* @vitest-environment jsdom */

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { WorldMapPickerModal } from './WorldMapPickerModal';

const serviceMocks = vi.hoisted(() => ({
  geocodeQuery: vi.fn(),
  reverseGeocode: vi.fn(),
}));

vi.mock('../../services/api', () => serviceMocks);

vi.mock('leaflet', () => {
  const map = {
    invalidateSize: vi.fn(),
    on: vi.fn(),
    remove: vi.fn(),
    setView: vi.fn(),
  };
  map.setView.mockReturnValue(map);

  const marker = {
    addTo: vi.fn(),
    getLatLng: vi.fn(() => ({ lat: 1.284, lng: 103.8513 })),
    on: vi.fn(),
    setLatLng: vi.fn(),
  };
  marker.addTo.mockReturnValue(marker);

  const layer = { addTo: vi.fn() };
  layer.addTo.mockReturnValue(layer);

  return {
    default: {
      control: { zoom: vi.fn(() => layer) },
      divIcon: vi.fn(options => options),
      map: vi.fn(() => map),
      marker: vi.fn(() => marker),
      tileLayer: vi.fn(() => layer),
    },
  };
});

let root: Root | undefined;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = undefined;
  document.body.replaceChildren();
  vi.clearAllMocks();
});

describe('WorldMapPickerModal', () => {
  it('keeps Enter search while using the shared modal action styles', async () => {
    serviceMocks.geocodeQuery.mockResolvedValue([{
      display_name: 'Raffles Place, Singapore',
      lat: 1.284,
      lng: 103.8513,
      supported_region: 'SINGAPORE',
    }]);

    const container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
    await act(async () => {
      root?.render(
        <WorldMapPickerModal
          isOpen
          onClose={vi.fn()}
          onSelectLocation={vi.fn()}
        />,
      );
    });

    const form = document.querySelector<HTMLFormElement>('.world-map-picker__search-form');
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]');
    const input = form?.querySelector<HTMLInputElement>('input');
    expect(form?.parentElement).toBe(dialog);
    expect(dialog?.style.borderRadius).toBe('var(--radius-lg)');
    expect(dialog?.style.gap).toBe('16px');
    expect(dialog?.style.padding).toBe('16px');
    expect(dialog?.style.background).toBe('var(--warm-card)');
    expect(dialog?.querySelector('[aria-label="Close location picker"]')).toBeNull();
    expect(dialog?.querySelector('section')).toBeNull();
    expect(dialog?.querySelector('.world-map-picker__map')).not.toBeNull();
    expect(input?.classList.contains('world-map-picker__search-input')).toBe(true);
    expect(form?.querySelector('button[type="submit"]')).toBeNull();

    await act(async () => {
      const valueSetter = Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype,
        'value',
      )?.set;
      valueSetter?.call(input, 'Raffles Place');
      input?.dispatchEvent(new Event('input', { bubbles: true }));
      input?.dispatchEvent(new KeyboardEvent('keydown', {
        bubbles: true,
        cancelable: true,
        key: 'Enter',
      }));
    });

    expect(serviceMocks.geocodeQuery).toHaveBeenCalledWith('Raffles Place', 1);
    const confirm = document.querySelector<HTMLButtonElement>('.world-map-picker__confirm');
    const cancel = document.querySelector<HTMLButtonElement>('.world-map-picker__cancel');
    expect(confirm?.classList.contains('ui-button-primary')).toBe(true);
    expect(confirm?.querySelector('svg')?.getAttribute('width')).toBe('18');
    expect(cancel).not.toBeNull();
  });
});
