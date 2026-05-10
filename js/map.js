import { MAP_CENTER, MAP_ZOOM } from './config.js?v=22';

const TILE_STYLES = {
  dark: {
    url: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors © <a href="https://carto.com/attributions">CARTO</a>',
    subdomains: 'abcd',
    maxZoom: 19,
  },
  light: {
    url: 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    subdomains: 'abc',
    maxZoom: 19,
  },
};

let _map       = null;
let _tileLayer = null;

export function initMap() {
  const map = _map = L.map('map', {
    center: MAP_CENTER,
    zoom: MAP_ZOOM,
    zoomControl: true,
    minZoom: 5,
    maxBounds: [[37, 5], [63, 35]],
    maxBoundsViscosity: 1.0,
  });
  _applyTiles(localStorage.getItem('mapymeteo_map_style') || 'light');

  map.createPane('coveragePane');
  map.getPane('coveragePane').style.zIndex  = 300; // między tiles(200) a imageOverlay(400)
  map.getPane('coveragePane').style.pointerEvents = 'none';

  return map;
}

function _applyTiles(style) {
  const t = TILE_STYLES[style] ?? TILE_STYLES.dark;
  if (_tileLayer) _tileLayer.remove();
  _tileLayer = L.tileLayer(t.url, {
    attribution: t.attribution,
    subdomains:  t.subdomains,
    maxZoom:     t.maxZoom,
  }).addTo(_map);
}

export function setMapStyle(style) {
  localStorage.setItem('mapymeteo_map_style', style);
  _applyTiles(style);
}

// ── Overlay radarowy ──────────────────────────────────────────────────────────
let overlay = null;

export function showFrame(map, frame, opacity, showMask = true, useRectMask = false) {
  const bounds = L.latLngBounds(frame.bounds[0], frame.bounds[1]);
  const src    = frame.image + '?t=' + frame.timestamp;

  if (overlay && !overlay.getBounds().equals(bounds, 0.001)) {
    overlay.remove();
    overlay = null;
  }

  if (overlay) {
    overlay.setUrl(src);
    overlay.setOpacity(opacity);
  } else {
    overlay = L.imageOverlay(src, bounds, { opacity, interactive: false }).addTo(map);
  }

  if (showMask) {
    _updateCoverageMask(map, frame.bounds, useRectMask);
  } else {
    clearCoverageMask();
  }
}

export function clearOverlay() {
  if (overlay) { overlay.remove(); overlay = null; }
  clearCoverageMask();
}

export function setOverlayOpacity(opacity) {
  if (overlay) overlay.setOpacity(opacity);
}

// ── Maska zasięgu ─────────────────────────────────────────────────────────────
let coverageMask     = null;
let maskBoundsKey    = null;

function _circleRing(lat, lon, km, n = 72) {
  const R  = 6371;
  const d  = km / R;
  const φ1 = lat * Math.PI / 180;
  const λ1 = lon * Math.PI / 180;
  const pts = [];
  for (let i = 0; i <= n; i++) {
    const θ  = (i / n) * 2 * Math.PI;
    const φ2 = Math.asin(Math.sin(φ1) * Math.cos(d) + Math.cos(φ1) * Math.sin(d) * Math.cos(θ));
    const λ2 = λ1 + Math.atan2(Math.sin(θ) * Math.sin(d) * Math.cos(φ1), Math.cos(d) - Math.sin(φ1) * Math.sin(φ2));
    pts.push([φ2 * 180 / Math.PI, λ2 * 180 / Math.PI]);
  }
  return pts;
}

function _rectRing(swLat, swLon, neLat, neLon) {
  return [
    [swLat, swLon],
    [swLat, neLon],
    [neLat, neLon],
    [neLat, swLon],
    [swLat, swLon],
  ];
}

function _updateCoverageMask(map, bounds, useRect = false) {
  const key = bounds[0][0] + ',' + bounds[0][1] + ',' + bounds[1][0] + ',' + bounds[1][1] + (useRect ? 'r' : 'c');
  if (key === maskBoundsKey && coverageMask) return;
  maskBoundsKey = key;

  const swLat = bounds[0][0], swLon = bounds[0][1];
  const neLat = bounds[1][0], neLon = bounds[1][1];

  const outerRing = [[90, -180], [90, 180], [-90, 180], [-90, -180]];
  const innerRing = useRect
    ? _rectRing(swLat, swLon, neLat, neLon)
    : _circleRing((swLat + neLat) / 2, (swLon + neLon) / 2, (neLat - swLat) / 2 * 111.32);

  if (coverageMask) {
    coverageMask.setLatLngs([outerRing, innerRing]);
  } else {
    coverageMask = L.polygon([outerRing, innerRing], {
      pane:        'coveragePane',
      fillColor:   '#0d1b2e',
      fillOpacity: 0.55,
      stroke:      false,
      interactive: false,
    }).addTo(map);
  }
}

export function clearCoverageMask() {
  if (coverageMask) { coverageMask.remove(); coverageMask = null; }
  maskBoundsKey = null;
}

// ── Znaczniki radarów ─────────────────────────────────────────────────────────
const dotEls = {};

export function addRadarMarkers(map, stations, onSelect) {
  stations.forEach(st => {
    const el = document.createElement('div');
    el.className = 'radar-dot';
    el.title     = st.name;
    dotEls[st.id] = el;

    L.marker([st.lat, st.lon], {
      icon: L.divIcon({ className: '', html: el, iconAnchor: [5, 5] }),
    }).addTo(map).on('click', () => onSelect(st.id));
  });
}

export function setActiveMarker(stationId) {
  Object.entries(dotEls).forEach(([id, el]) =>
    el.classList.toggle('active', id === stationId)
  );
}
