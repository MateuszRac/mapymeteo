/**
 * lightning.js — obsługa warstwy wyładowań atmosferycznych (MTG-LI).
 *
 * Tryby:
 *   'off'   — warstwa ukryta
 *   'radar' — tylko wyładowania dla aktualnego okna radarowego (T-10min…T)
 *   '3h'    — wszystkie z ostatnich 3 godzin, kolor wg wieku
 */

const LIGHTNING_URL = './img/polrad/lightning.json';
const SLOT_MS       = 10 * 60 * 1000;   // 10 minut
const HISTORY_MS    = 3 * 60 * 60 * 1000;

let _data    = null;    // załadowany lightning.json
let _layer   = null;    // L.LayerGroup
let _mode    = 'radar'; // 'off' | 'radar' | '3h'
let _opacity = 0.80;    // fillOpacity (0–1)

// ── Pomocnicze ────────────────────────────────────────────────────────────────

function _toMs(isoStr) {
  // Traktuje wszystkie timestampy w JSON jako UTC
  return new Date(isoStr.endsWith('Z') ? isoStr : isoStr + 'Z').getTime();
}

function _slotKey(tMs) {
  // Klucz slotu = koniec 10-minutowego okna (floor do pełnych 10 min)
  const floored = Math.floor(tMs / SLOT_MS) * SLOT_MS;
  const d = new Date(floored);
  const pad = n => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth()+1)}-${pad(d.getUTCDate())}` +
         `T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:00`;
}

// Kolor wg wieku (ms) dla trybu 3h
function _ageColor(ageMs) {
  const m = ageMs / 60000;
  if (m < 10)  return '#ffe000';
  if (m < 20)  return '#ffb800';
  if (m < 40)  return '#ff7800';
  if (m < 60)  return '#ff3c00';
  if (m < 120) return '#e00000';
  return '#990000';
}

// Promień kółka wg liczby eventów
function _radius(n) {
  return Math.min(3 + Math.sqrt(n) * 1.3, 14);
}

function _popup(g) {
  const dt = new Date(g.t.endsWith('Z') ? g.t : g.t + 'Z');
  const pad = n => String(n).padStart(2, '0');
  const timeStr = `${pad(dt.getUTCHours())}:${pad(dt.getUTCMinutes())}:${pad(dt.getUTCSeconds())} UTC`;
  const dateStr = `${pad(dt.getUTCDate())}.${pad(dt.getUTCMonth()+1)}.${dt.getUTCFullYear()}`;
  return `<div class="lgtn-popup">
    <b>Wyładowanie atmosferyczne</b>
    <table>
      <tr><td>Czas</td><td>${dateStr} ${timeStr}</td></tr>
      <tr><td>Szerokość</td><td>${g.lat.toFixed(3)}°N</td></tr>
      <tr><td>Długość</td><td>${g.lon.toFixed(3)}°E</td></tr>
      <tr><td>Liczba impulsów</td><td>${g.n}</td></tr>
    </table>
  </div>`;
}

// ── Publiczne API ─────────────────────────────────────────────────────────────

export function initLightningLayer(map) {
  _layer = L.layerGroup().addTo(map);
}

export async function loadLightning() {
  try {
    const r = await fetch(LIGHTNING_URL + '?t=' + Date.now());
    if (r.ok) _data = await r.json();
  } catch (_) {}
}

export async function refreshLightning() {
  try {
    const r = await fetch(LIGHTNING_URL + '?t=' + Date.now());
    if (r.ok) _data = await r.json();
  } catch (_) {}
}

export function getLightningMode()           { return _mode; }
export function setLightningMode(mode)       { _mode = mode; }
export function setLightningOpacity(opacity) { _opacity = opacity; }

/**
 * Renderuje wyładowania na mapie.
 * Zwraca { missing: bool } — true gdy slot istnieje w JSON z ok:false.
 */
export function renderLightning(radarTimestamp) {
  if (!_layer) return { missing: false };
  _layer.clearLayers();

  if (_mode === 'off' || !_data) return { missing: false };
  if (_mode === '3h')   return _render3h();
  return _renderRadar(radarTimestamp);
}

// ── Renderowanie ──────────────────────────────────────────────────────────────

function _renderRadar(radarTimestamp) {
  if (!radarTimestamp) return { missing: false };

  const tMs = _toMs(radarTimestamp);
  const sk  = _slotKey(tMs);
  const slot = _data.slots?.[sk];

  const missing = slot != null && !slot.ok;

  if (slot?.ok && slot.groups?.length) {
    for (const g of slot.groups) {
      L.circleMarker([g.lat, g.lon], {
        radius:      _radius(g.n),
        color:       '#c0a000',
        fillColor:   '#ffe000',
        fillOpacity: _opacity,
        weight:      1.2,
        opacity:     Math.min(_opacity + 0.15, 1),
      }).bindPopup(_popup(g), { maxWidth: 240 }).addTo(_layer);
    }
  }

  return { missing };
}

function _render3h() {
  if (!_data?.slots) return { missing: false };

  const now    = Date.now();
  const cutoff = now - HISTORY_MS;

  for (const slot of Object.values(_data.slots)) {
    if (!slot.ok || !slot.groups) continue;
    for (const g of slot.groups) {
      const gMs = _toMs(g.t);
      if (gMs < cutoff) continue;
      const color = _ageColor(now - gMs);
      L.circleMarker([g.lat, g.lon], {
        radius:      _radius(g.n),
        color:       '#00000040',
        fillColor:   color,
        fillOpacity: _opacity,
        weight:      0.8,
        opacity:     Math.min(_opacity + 0.15, 1),
      }).bindPopup(_popup(g), { maxWidth: 240 }).addTo(_layer);
    }
  }

  return { missing: false };
}
