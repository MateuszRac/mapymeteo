import { REFRESH_MS, MAP_CENTER, MAP_ZOOM } from './config.js?v=15';
import {
  loadAll, refreshManifest, buildIndex, parseKey,
  getStationLabel, getProductLabel,
} from './data.js?v=15';
import { initMap, showFrame, clearOverlay, setOverlayOpacity, addRadarMarkers, setActiveMarker } from './map.js?v=15';
import { createPlayer } from './player.js?v=15';

const SPEED_STEPS = [2000, 1200, 800, 500, 250];

// ── DOM ───────────────────────────────────────────────────────────────────────
const stationSel       = document.getElementById('station-select');
const productBtns      = document.getElementById('product-btns');
const curProdLabel     = document.getElementById('cur-prod-label');
const btnProductToggle = document.getElementById('btn-product-toggle');
const loadingEl        = document.getElementById('loading');
const timeClock        = document.getElementById('time-clock');
const timeDate         = document.getElementById('time-date');
const sidePanel        = document.getElementById('side-panel');
const spTitle          = document.getElementById('sp-title');
const spInfoName       = document.getElementById('sp-info-name');
const spInfoDesc       = document.getElementById('sp-info-desc');
const opacitySl        = document.getElementById('opacity-slider');
const opacityVal       = document.getElementById('opacity-val');
const speedSl          = document.getElementById('speed-slider');
const colorbarEl       = document.getElementById('colorbar');
const colorbarBar      = document.getElementById('colorbar-bar');
const colorbarTicks    = document.getElementById('colorbar-ticks');
const colorbarLabel    = document.getElementById('colorbar-label');
const toolbarEl        = document.getElementById('toolbar');

const SP_PAGES = {
  info:     document.getElementById('sp-info'),
  settings: document.getElementById('sp-settings'),
  copy:     document.getElementById('sp-copy'),
  support:  document.getElementById('sp-support'),
};
const SP_TITLES = { info: 'O produkcie', settings: 'Ustawienia', copy: 'Prawa autorskie', support: 'Wsparcie' };

// ── Stan ──────────────────────────────────────────────────────────────────────
let manifest    = null;
let config      = null;
let index       = null;
let map         = null;
let player      = null;
let productInfo = {};
let palettes    = {};

let selStation = null;
let selKey     = null;

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
  initCookies();
  try {
    ({ config, manifest, productInfo, palettes } = await loadAll());
    index  = buildIndex(manifest, config);
    map    = initMap();
    player = createPlayer({
      onFrame: frame => {
        showFrame(map, frame, parseInt(opacitySl.value, 10) / 100);
        updateTimeDisplay(frame.timestamp);
        updateColorbar(frame.quantity ?? (selKey ? parseKey(selKey).unit : null));
      },
      onClear: () => {
        clearOverlay();
        updateTimeDisplay(null);
      },
    });
    addRadarMarkers(map, config.radar_stations || [], id => selectStation(id));
    populateStations();
    selectFirstAvailable();
    positionColorbar();
    if (window.ResizeObserver) {
      new ResizeObserver(positionColorbar).observe(toolbarEl);
    } else {
      window.addEventListener('resize', positionColorbar);
    }
  } catch (e) {
    console.error(e);
  } finally {
    loadingEl.classList.add('hidden');
  }
}

// ── Czas UTC → Warszawa ───────────────────────────────────────────────────────
function updateTimeDisplay(isoStr) {
  if (!isoStr) {
    timeClock.textContent = '--:--';
    timeDate.textContent  = '--.--.----';
    return;
  }
  const dt = new Date(isoStr.includes('Z') ? isoStr : isoStr + 'Z');
  timeClock.textContent = dt.toLocaleTimeString('pl-PL', { timeZone: 'Europe/Warsaw', hour: '2-digit', minute: '2-digit' });
  timeDate.textContent  = dt.toLocaleDateString('pl-PL',  { timeZone: 'Europe/Warsaw', day: '2-digit', month: '2-digit', year: 'numeric' });
}

// ── Cookies ───────────────────────────────────────────────────────────────────
function initCookies() {
  const banner = document.getElementById('cookie-banner');
  if (!banner) return;
  if (localStorage.getItem('mapymeteo_cookies_ok')) { banner.classList.add('hidden'); return; }
  document.getElementById('cookie-accept')?.addEventListener('click', () => {
    localStorage.setItem('mapymeteo_cookies_ok', '1');
    banner.classList.add('hidden');
  });
}

// ── Lista stacji ──────────────────────────────────────────────────────────────
function populateStations() {
  stationSel.innerHTML = '';
  const compoIds = index.stationIds.filter(id => id === 'COMPO');
  const radarIds = index.stationIds.filter(id => id !== 'COMPO');

  compoIds.forEach(id => stationSel.appendChild(makeOption(id, getStationLabel(id, config))));
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

// ── Etykieta produktu ─────────────────────────────────────────────────────────
function itemLabel(item) {
  let prod = getProductLabel(item.productType, config);
  if (item.isCompo && prod.startsWith('Polska - ')) prod = prod.slice('Polska - '.length);
  prod = prod.charAt(0).toUpperCase() + prod.slice(1);
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
  if (first) selectStation(first, false);
}

function selectStation(id, panMap = true) {
  selStation = id;
  stationSel.value = id;
  setActiveMarker(id);
  rebuildProductBtns(id);

  if (panMap && map) {
    if (id === 'COMPO') {
      map.flyTo(MAP_CENTER, MAP_ZOOM, { duration: 0.8 });
    } else {
      const st = (config.radar_stations || []).find(r => r.id === id);
      if (st) map.flyTo([st.lat, st.lon], 8, { duration: 0.8 });
    }
  }

  const items = index.byStation[id]?.items ?? [];
  if (!items.length) { selectKey(null); return; }

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
  updateCurProdLabel(key);
  closeProductDropdown();
  const unit = (key ? parseKey(key).unit : null)
    ?? (key ? (manifest?.products[key]?.frames?.[0]?.quantity ?? null) : null);
  updateColorbar(unit);
  applySelection();
}

function applySelection() {
  if (!selKey) { player?.loadFrames([]); return; }
  const frames = manifest.products[selKey]?.frames ?? [];
  player?.loadFrames(frames);
}

function updateCurProdLabel(key) {
  if (!curProdLabel) return;
  if (!key) { curProdLabel.textContent = '—'; return; }
  const parsed = parseKey(key);
  const items  = index.byStation[parsed.stationId]?.items ?? [];
  const item   = items.find(i => i.key === key);
  curProdLabel.textContent = item ? itemLabel(item) : parsed.productType;
}

// ── Pozycjonowanie paska kolorów pod toolbarem ────────────────────────────────
function positionColorbar() {
  if (colorbarEl && toolbarEl) {
    colorbarEl.style.top = toolbarEl.offsetHeight + 'px';
  }
}

// ── Pasek kolorów ─────────────────────────────────────────────────────────────
function updateColorbar(unit) {
  const pal = unit ? (palettes[unit] ?? palettes[unit.toUpperCase()] ?? null) : null;
  if (!pal || !colorbarEl) {
    colorbarEl?.classList.add('hidden');
    return;
  }
  colorbarEl.classList.remove('hidden');

  const colors = pal.colors;
  const n = colors.length;
  let gradient;
  if (pal.discrete) {
    const stops = [];
    for (let i = 0; i < n; i++) {
      const p1 = (i / n * 100).toFixed(3);
      const p2 = ((i + 1) / n * 100).toFixed(3);
      stops.push(`${colors[i]} ${p1}%`, `${colors[i]} ${p2}%`);
    }
    gradient = `linear-gradient(to right, ${stops.join(',')})`;
  } else {
    const stops = colors.map((c, i) => `${c} ${(i / (n - 1) * 100).toFixed(1)}%`);
    gradient = `linear-gradient(to right, ${stops.join(',')})`;
  }
  if (colorbarBar) colorbarBar.style.background = gradient;

  if (colorbarTicks) {
    colorbarTicks.innerHTML = '';
    const last = pal.ticks.length - 1;
    pal.ticks.forEach((tick, idx) => {
      const span = document.createElement('span');
      span.className = 'cb-tick';
      span.style.left = tick.pct + '%';
      if (idx === 0)    span.style.transform = 'none';
      else if (idx === last) span.style.transform = 'translateX(-100%)';
      span.textContent = tick.label;
      colorbarTicks.appendChild(span);
    });
  }

  if (colorbarLabel) colorbarLabel.textContent = pal.label;
}

// ── Dropdown produktów (mobile) ───────────────────────────────────────────────
function closeProductDropdown() {
  productBtns?.classList.remove('open');
  btnProductToggle?.classList.remove('active');
}

btnProductToggle?.addEventListener('click', () => {
  productBtns?.classList.toggle('open');
  btnProductToggle?.classList.toggle('active');
  if (sidePanel?.classList.contains('open')) closePanel();
});

// ── Panel boczny ──────────────────────────────────────────────────────────────
function openPanel(name) {
  Object.entries(SP_PAGES).forEach(([k, el]) => el?.classList.toggle('active', k === name));
  if (spTitle) spTitle.textContent = SP_TITLES[name] ?? '';
  if (name === 'info') updateInfoPanel();
  sidePanel?.classList.add('open');
  document.querySelectorAll('.side-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.panel === name)
  );
}

function closePanel() {
  sidePanel?.classList.remove('open');
  document.querySelectorAll('.side-btn').forEach(b => b.classList.remove('active'));
}

function updateInfoPanel() {
  if (!selKey) {
    if (spInfoName) spInfoName.textContent = '—';
    if (spInfoDesc) spInfoDesc.textContent = 'Wybierz produkt, aby zobaczyć jego opis.';
    return;
  }
  const parsed = parseKey(selKey);
  const lookupKey = parsed.productType + '__' + parsed.unit;
  const info = productInfo[lookupKey];

  const items = index.byStation[parsed.stationId]?.items ?? [];
  const item  = items.find(i => i.key === selKey);
  if (spInfoName) spInfoName.textContent = info?.name ?? (item ? itemLabel(item) : lookupKey);
  if (spInfoDesc) spInfoDesc.textContent = info?.description ?? 'Brak szczegółowego opisu dla tego produktu.';
}

document.querySelectorAll('.side-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const name = btn.dataset.panel;
    const alreadyOpen = sidePanel?.classList.contains('open')
      && document.querySelector('.side-btn.active')?.dataset.panel === name;
    closeProductDropdown();
    alreadyOpen ? closePanel() : openPanel(name);
  });
});

document.getElementById('side-panel-close')?.addEventListener('click', closePanel);

// Klik na mapę zamyka panel i dropdown
document.getElementById('map')?.addEventListener('click', () => {
  closePanel();
  closeProductDropdown();
});

// ── Eventy ────────────────────────────────────────────────────────────────────
stationSel.addEventListener('change', () => selectStation(stationSel.value));

opacitySl?.addEventListener('input', () => {
  setOverlayOpacity(parseInt(opacitySl.value, 10) / 100);
  if (opacityVal) opacityVal.textContent = opacitySl.value + '%';
});

speedSl?.addEventListener('input', () =>
  player?.setSpeed(SPEED_STEPS[parseInt(speedSl.value, 10) - 1])
);

// ── Auto-odświeżanie ──────────────────────────────────────────────────────────
setInterval(async () => {
  try {
    manifest = await refreshManifest();
    index    = buildIndex(manifest, config);

    const saved = selStation;
    populateStations();
    stationSel.value = saved;

    if (selKey) {
      const frames = manifest.products[selKey]?.frames ?? [];
      player?.updateFrames(frames);
    }
  } catch (_) {}
}, REFRESH_MS);

// ── Start ─────────────────────────────────────────────────────────────────────
init();
