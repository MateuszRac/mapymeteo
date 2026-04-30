import { REFRESH_MS } from './config.js';
import {
  loadAll, refreshManifest, buildIndex, parseKey,
  getStationLabel, getProductLabel,
} from './data.js';
import { initMap, showFrame, clearOverlay, setOverlayOpacity, addRadarMarkers, setActiveMarker } from './map.js';
import { createPlayer } from './player.js';

// Prędkości animacji: suwak 1..5 = wolno..szybko
const SPEED_STEPS = [2000, 1200, 800, 500, 250];

// ── DOM ───────────────────────────────────────────────────────────────────────
const stationSel  = document.getElementById('station-select');
const productBtns = document.getElementById('product-btns');
const opacitySl   = document.getElementById('opacity-slider');
const speedSl     = document.getElementById('speed-slider');
const statusEl    = document.getElementById('status');
const loadingEl   = document.getElementById('loading');

// ── Stan ──────────────────────────────────────────────────────────────────────
let manifest  = null;
let config    = null;
let index     = null;
let map       = null;
let player    = null;

let selStation = null;
let selKey     = null;   // pełny klucz manifestu, np. "BRZ_0_5.ppi__DBZH"

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
  try {
    ({ config, manifest } = await loadAll());
    index  = buildIndex(manifest, config);
    map    = initMap();
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

  compoIds.forEach(id =>
    stationSel.appendChild(makeOption(id, getStationLabel(id, config)))
  );
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

// ── Etykieta przycisku: produkt + jednostka ───────────────────────────────────
function itemLabel(item) {
  let prod = getProductLabel(item.productType, config);
  // Dla COMPO (stacja "Polska") usuń redundantny prefix "Polska - "
  if (item.isCompo && prod.startsWith('Polska - ')) {
    prod = prod.slice('Polska - '.length);
  }
  // Capitalize
  prod = prod.charAt(0).toUpperCase() + prod.slice(1);
  // Dołącz kod jednostki — zwięzły identyfikator (DBZH, RATE, VRADH…)
  return item.unit ? `${prod} · ${item.unit}` : prod;
}

// ── Przyciski produktów ───────────────────────────────────────────────────────
function rebuildProductBtns(stationId) {
  productBtns.innerHTML = '';
  const items = index.byStation[stationId]?.items ?? [];
  if (!items.length) {
    productBtns.innerHTML = '<span class="no-data-msg">brak danych</span>';
    return;
  }
  items.forEach(item => {
    const btn = document.createElement('button');
    btn.className   = 'prod-btn';
    btn.dataset.key = item.key;
    btn.textContent = itemLabel(item);
    btn.addEventListener('click', () => selectKey(item.key));
    productBtns.appendChild(btn);
  });
}

function markActiveBtn(key) {
  productBtns.querySelectorAll('.prod-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.key === key)
  );
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

  const items = index.byStation[stationId]?.items ?? [];
  if (!items.length) { selectKey(null); return; }

  // Przy zmianie stacji próbuj zachować ten sam typ produktu i jednostkę
  let best = null;
  if (selKey) {
    const prev = parseKey(selKey);
    best = items.find(i => i.productType === prev.productType && i.unit === prev.unit)
        ?? items.find(i => i.productType === prev.productType)
        ?? null;
  }
  selectKey((best ?? items[0]).key);
}

function selectKey(key) {
  selKey = key;
  markActiveBtn(key);
  applySelection();
}

function applySelection() {
  if (!selKey) {
    statusEl.textContent = 'Brak danych';
    player.loadFrames([]);
    return;
  }
  const prod   = manifest.products[selKey];
  const frames = prod?.frames ?? [];
  statusEl.textContent = `${frames.length} ramek`;
  player.loadFrames(frames);
}

// ── Eventy ────────────────────────────────────────────────────────────────────
stationSel.addEventListener('change', () => selectStation(stationSel.value));

opacitySl.addEventListener('input', () =>
  setOverlayOpacity(parseInt(opacitySl.value, 10) / 100)
);

speedSl?.addEventListener('input', () =>
  player?.setSpeed(SPEED_STEPS[parseInt(speedSl.value, 10) - 1])
);

// ── Auto-odświeżanie ──────────────────────────────────────────────────────────
setInterval(async () => {
  try {
    const oldCount = selKey ? (manifest.products[selKey]?.frames?.length ?? 0) : 0;

    manifest = await refreshManifest();
    index    = buildIndex(manifest, config);

    const saved = selStation;
    populateStations();
    stationSel.value = saved;

    const newCount = selKey ? (manifest.products[selKey]?.frames?.length ?? 0) : 0;
    if (newCount !== oldCount) {
      applySelection();
      statusEl.textContent = `${newCount} ramek` +
        (newCount > oldCount ? ` (+${newCount - oldCount} nowych)` : '');
    }
  } catch (_) {}
}, REFRESH_MS);

// ── Start ─────────────────────────────────────────────────────────────────────
init();
