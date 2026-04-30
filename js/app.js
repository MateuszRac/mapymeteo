import { REFRESH_MS } from './config.js';
import {
  loadAll, refreshManifest, buildIndex,
  getStationLabel, getProductLabel, getUnitLabel, resolveKey,
} from './data.js';
import { initMap, showFrame, clearOverlay, setOverlayOpacity, addRadarMarkers, setActiveMarker } from './map.js';
import { createPlayer } from './player.js';

// ── DOM ───────────────────────────────────────────────────────────────────────
const stationSel  = document.getElementById('station-select');
const productBtns = document.getElementById('product-btns');
const unitSection = document.getElementById('unit-section');
const unitSel     = document.getElementById('unit-select');
const opacitySl   = document.getElementById('opacity-slider');
const statusEl    = document.getElementById('status');
const loadingEl   = document.getElementById('loading');

// ── Stan ──────────────────────────────────────────────────────────────────────
let manifest  = null;
let config    = null;
let index     = null;
let map       = null;
let player    = null;

let selStation = null;
let selProduct = null;
let selUnit    = null;

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
  try {
    ({ config, manifest } = await loadAll());
    index = buildIndex(manifest, config);
    map   = initMap();
    player = createPlayer({
      onFrame: frame => showFrame(map, frame, parseInt(opacitySl.value, 10) / 100),
      onClear: clearOverlay,
    });
    addRadarMarkers(map, config.radar_stations || [], id => selectStation(id));
    populateStations();
    selectFirstAvailable();
  } catch (e) {
    statusEl.textContent = 'Błąd: ' + e.message;
  } finally {
    loadingEl.classList.add('hidden');
  }
}

// ── Lista stacji ──────────────────────────────────────────────────────────────
function populateStations() {
  stationSel.innerHTML = '';

  const compoIds = index.stationIds.filter(id => id === 'COMPO');
  const radarIds = index.stationIds.filter(id => id !== 'COMPO');

  if (compoIds.length) {
    compoIds.forEach(id => stationSel.appendChild(makeOption(id, getStationLabel(id, config))));
  }
  if (radarIds.length) {
    const og = document.createElement('optgroup');
    og.label = 'Radary';
    radarIds.forEach(id => og.appendChild(makeOption(id, getStationLabel(id, config))));
    stationSel.appendChild(og);
  }
}

function makeOption(value, label) {
  const o = document.createElement('option');
  o.value = value; o.textContent = label;
  return o;
}

// ── Przyciski produktów ───────────────────────────────────────────────────────
function rebuildProductBtns(stationId) {
  productBtns.innerHTML = '';
  const station = index.byStation[stationId];
  if (!station) {
    productBtns.innerHTML = '<span class="no-data-msg">brak danych</span>';
    return;
  }
  Object.keys(station.products).forEach(pt => {
    const btn = document.createElement('button');
    btn.className    = 'prod-btn';
    btn.dataset.pt   = pt;
    btn.textContent  = getProductLabel(pt, config);
    btn.addEventListener('click', () => selectProduct(pt));
    productBtns.appendChild(btn);
  });
}

function markActiveProduct(pt) {
  productBtns.querySelectorAll('.prod-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.pt === pt)
  );
}

// ── Selector jednostek ────────────────────────────────────────────────────────
function rebuildUnitSelector(stationId, productType) {
  const prod  = index.byStation[stationId]?.products?.[productType];
  const units = prod?.units ?? [];

  // Filtruj puste (produkty bez jednostki)
  const realUnits = units.filter(u => u !== '');

  if (realUnits.length <= 1) {
    unitSection.style.display = 'none';
    return;
  }

  unitSection.style.display = 'flex';
  unitSel.innerHTML = '';
  realUnits.forEach(u => {
    unitSel.appendChild(makeOption(u, getUnitLabel(u, config)));
  });
  if (selUnit && realUnits.includes(selUnit)) unitSel.value = selUnit;
  else unitSel.value = realUnits[0];
}

// ── Logika wyboru ─────────────────────────────────────────────────────────────
function selectFirstAvailable() {
  const first = index.stationIds[0];
  if (first) selectStation(first);
}

function selectStation(stationId) {
  selStation = stationId;
  stationSel.value = stationId;
  setActiveMarker(stationId);
  rebuildProductBtns(stationId);

  const availTypes = Object.keys(index.byStation[stationId]?.products ?? {});
  const keepProd   = selProduct && availTypes.includes(selProduct) ? selProduct : availTypes[0];
  selectProduct(keepProd);
}

function selectProduct(productType) {
  if (!productType) { player.loadFrames([]); return; }
  selProduct = productType;
  markActiveProduct(productType);

  const prod    = index.byStation[selStation]?.products?.[productType];
  const units   = (prod?.units ?? []).filter(u => u !== '');
  const keepUnit = selUnit && units.includes(selUnit) ? selUnit : (units[0] ?? null);

  rebuildUnitSelector(selStation, productType);
  selUnit = keepUnit;
  if (keepUnit) unitSel.value = keepUnit;

  applySelection();
}

function applySelection() {
  const key  = resolveKey(index, selStation, selProduct, selUnit);
  if (!key) { statusEl.textContent = 'Brak danych'; player.loadFrames([]); return; }
  const prod   = manifest.products[key];
  const frames = prod?.frames ?? [];
  statusEl.textContent = `${frames.length} ramek`;
  player.loadFrames(frames);
}

// ── Eventy ────────────────────────────────────────────────────────────────────
stationSel.addEventListener('change', () => selectStation(stationSel.value));
unitSel.addEventListener('change', () => {
  selUnit = unitSel.value;
  applySelection();
});
opacitySl.addEventListener('input', () =>
  setOverlayOpacity(parseInt(opacitySl.value, 10) / 100)
);

// ── Auto-odświeżanie ──────────────────────────────────────────────────────────
setInterval(async () => {
  try {
    const oldKey   = resolveKey(index, selStation, selProduct, selUnit);
    const oldCount = oldKey ? (manifest.products[oldKey]?.frames?.length ?? 0) : 0;

    manifest = await refreshManifest();
    index    = buildIndex(manifest, config);

    const saved = selStation;
    populateStations();
    stationSel.value = saved;

    const newKey   = resolveKey(index, selStation, selProduct, selUnit);
    const newCount = newKey ? (manifest.products[newKey]?.frames?.length ?? 0) : 0;

    if (newCount !== oldCount) {
      applySelection();
      statusEl.textContent = `${newCount} ramek` +
        (newCount > oldCount ? ` (+${newCount - oldCount} nowych)` : '');
    }
  } catch (_) {}
}, REFRESH_MS);

// ── Start ─────────────────────────────────────────────────────────────────────
init();
