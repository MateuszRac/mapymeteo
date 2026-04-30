import { MANIFEST_URL, CONFIG_URL } from './config.js';

export async function loadAll() {
  const [config, manifest] = await Promise.all([
    fetch(CONFIG_URL   + '?t=' + Date.now()).then(r => { if (!r.ok) throw new Error(r.statusText); return r.json(); }),
    fetch(MANIFEST_URL + '?t=' + Date.now()).then(r => { if (!r.ok) throw new Error(r.statusText); return r.json(); }),
  ]);
  return { config, manifest };
}

export async function refreshManifest() {
  const r = await fetch(MANIFEST_URL + '?t=' + Date.now());
  return r.json();
}

/**
 * Rozbija klucz manifestu na składowe.
 * Formaty: "BRZ_0_5.ppi__dBZ"  lub  "COMPO_CMAX_250__dBZ"
 */
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

/**
 * Buduje indeks ze struktury manifestu.
 * Wynik:
 *   stationIds: ['COMPO','brz','gdy',...]   — stacje które mają dane
 *   byStation[id].products[productType].units = [unit, ...]
 *   byStation[id].products[productType].keys  = { unit: manifestKey }
 */
export function buildIndex(manifest, config) {
  const byStation   = {};
  const stationOrder = ['COMPO', ...(config.radar_stations || []).map(s => s.id)];

  for (const key of Object.keys(manifest.products ?? {})) {
    const p = parseKey(key);
    if (!byStation[p.stationId]) byStation[p.stationId] = { products: {} };
    const prods = byStation[p.stationId].products;
    if (!prods[p.productType]) prods[p.productType] = { units: [], keys: {} };
    const entry = prods[p.productType];
    const uKey  = p.unit ?? '';
    if (!entry.units.includes(uKey)) entry.units.push(uKey);
    entry.keys[uKey] = key;
  }

  return {
    byStation,
    stationIds: stationOrder.filter(id => byStation[id]),
  };
}

export function getStationLabel(stationId, config) {
  if (stationId === 'COMPO') return 'Kompozyty ogólnopolskie';
  const s = (config.radar_stations || []).find(r => r.id === stationId);
  return s ? `${s.name}` : stationId.toUpperCase();
}

export function getProductLabel(productType, config) {
  if (config.product_labels?.[productType]) return config.product_labels[productType];
  // Kompozyty: "CMAX_250" → klucz "CMAX_250.comp.cmax"
  const full = (config.compo_products || []).find(c => c.split('.')[0] === productType);
  if (full) return config.product_labels?.[full] || full;
  return productType;
}

export function getUnitLabel(unit, config) {
  if (!unit) return '—';
  return config.unit_labels?.[unit] || unit;
}

/**
 * Próbuje dopasować klucz manifestu dla (stationId, productType, unit).
 * Jeśli nie ma dokładnego dopasowania:
 *   1. Ten sam productType, inna jednostka
 *   2. Pierwszy dostępny produkt stacji
 */
export function resolveKey(index, stationId, productType, unit) {
  const station = index.byStation[stationId];
  if (!station) return null;

  const prod = station.products[productType];
  if (prod) {
    const uKey = unit ?? '';
    if (prod.keys[uKey] !== undefined) return prod.keys[uKey];
    const firstUnit = prod.units[0];
    if (firstUnit !== undefined && prod.keys[firstUnit] !== undefined) return prod.keys[firstUnit];
  }

  // Fallback: pierwszy produkt stacji
  const firstType = Object.keys(station.products)[0];
  if (!firstType) return null;
  const fallback   = station.products[firstType];
  const firstUnit  = fallback.units[0];
  return firstUnit !== undefined ? (fallback.keys[firstUnit] ?? null) : null;
}
