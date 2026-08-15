import { describe, expect, it } from 'vitest';
import corridorHotspotCatalog from '../../../backend/data/corridor_hotspots.json';
import {
  BATAM_HOTSPOT_LIMIT,
  BATAM_HOTSPOT_REFERENCES,
  BATAM_LOCATION_PHOTOS,
} from './batamHotspots';

describe('Batam congestion hotspot reference catalogue', () => {
  it('publishes exactly 30 planning locations with the requested 20/10 split', () => {
    expect(BATAM_HOTSPOT_REFERENCES).toHaveLength(BATAM_HOTSPOT_LIMIT);
    expect(BATAM_HOTSPOT_REFERENCES.filter(item => item.priority === 'CRITICAL')).toHaveLength(20);
    expect(BATAM_HOTSPOT_REFERENCES.filter(item => item.priority === 'HEAVY')).toHaveLength(10);
  });

  it('keeps every id unique and every marker inside the Batam-area map extent', () => {
    const ids = BATAM_HOTSPOT_REFERENCES.map(item => item.id);
    expect(new Set(ids).size).toBe(ids.length);

    BATAM_HOTSPOT_REFERENCES.forEach((item) => {
      expect(item.lat).toBeGreaterThanOrEqual(0.98);
      expect(item.lat).toBeLessThanOrEqual(1.21);
      expect(item.lng).toBeGreaterThanOrEqual(103.91);
      expect(item.lng).toBeLessThanOrEqual(104.15);
      expect(item.observed).toBe(false);
      expect(item.basis).toBe('modelled-planning-priority');
    });
  });

  it('keeps licensed photo metadata aligned with the backend-owned catalogue', () => {
    const frontendIds = BATAM_HOTSPOT_REFERENCES.map(item => item.id).sort();
    const backendIds = corridorHotspotCatalog.candidates
      .map(item => item.zone_id)
      .sort();

    expect(frontendIds).toEqual(backendIds);
    expect(frontendIds).not.toContain('zone-tembesi');
    expect(frontendIds).toContain('zone-batu-aji');
  });

  it('attaches source, author, licence, alt text and context to every photo', () => {
    Object.values(BATAM_LOCATION_PHOTOS).forEach((photo) => {
      expect(photo.imageUrl).toMatch(/^https:\/\/commons\.wikimedia\.org\//);
      expect(photo.sourceUrl).toMatch(/^https:\/\/commons\.wikimedia\.org\//);
      expect(photo.author.length).toBeGreaterThan(0);
      expect(photo.licenseUrl).toMatch(/^https:\/\//);
      expect(photo.alt.length).toBeGreaterThan(12);
    });

    BATAM_HOTSPOT_REFERENCES.forEach((item) => {
      expect(item.photoContext).toMatch(/photo$/i);
      expect(item.photo.sourceUrl).toMatch(/^https:\/\/commons\.wikimedia\.org\//);
    });
  });
});
