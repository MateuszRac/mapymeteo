import { MANIFEST_URL, CONFIG_URL, PRODUCTS_URL, PALETTES_URL } from './config.js?v=20';

export async function loadAll() {
  const [config, manifest, products, palettes] = await Promise.all([
    fetch(CONFIG_URL   + '?t=' + Date.now()).then(r => { if (!r.ok) throw new Error(r.statusText); return r.json(); }),
    fetch(MANIFEST_URL + '?t=' + Date.now()).then(r => { if (!r.ok) throw new Error(r.statusText); return r.json(); }),
    fetch(PRODUCTS_URL + '?t=' + Date.now()).then(r => r.ok ? r.json() : {}),
    fetch(PALETTES_URL + '?t=' + Date.now()).then(r => r.ok ? r.json() : {}),
  ]);
  return { config, manifest, products, palettes };
}

export async function refreshManifest() {
  const r = await fetch(MANIFEST_URL + '?t=' + Date.now());
  return r.json();
}

export function parseKey(key) {
  const dbl    = key.indexOf('__');
  const prefix = dbl >= 0 ? key.slice(0, dbl) : key;
  const unit   = dbl >= 0 ? key.slice(dbl + 2) : null;

  if (prefix.startsWith('COMPO_')) {
    return { isCompo: true, stationId: 'COMPO', productType: prefix.slice(6), unit, key };
  }
  const sep = prefix.indexOf('_');
  return {
    isCompo:     false,
    stationId:   prefix.slice(0, sep).toLowerCase(),
    productType: prefix.slice(sep + 1),
    unit,
    key,
  };
}

export function buildIndex(manifest, config) {
  const byStation    = {};
  const stationOrder = ['COMPO', ...(config.radar_stations || []).map(s => s.id)];

  for (const key of Object.keys(manifest.products ?? {})) {
    const p = parseKey(key);
    if (!byStation[p.stationId]) byStation[p.stationId] = { items: [] };
    byStation[p.stationId].items.push({
      key,
      productType: p.productType,
      unit:        p.unit,
      isCompo:     p.isCompo,
    });
  }

  return {
    byStation,
    stationIds: stationOrder.filter(id => byStation[id]),
  };
}

export function getStationLabel(stationId, config) {
  if (stationId === 'COMPO') return 'Polska';
  const s = (config.radar_stations || []).find(r => r.id === stationId);
  return s ? s.name : stationId.toUpperCase();
}
