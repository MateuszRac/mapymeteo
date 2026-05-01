import { MAP_CENTER, MAP_ZOOM } from './config.js?v=15';

export function initMap() {
  const map = L.map('map', { center: MAP_CENTER, zoom: MAP_ZOOM, zoomControl: true });
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    maxZoom: 19,
  }).addTo(map);
  return map;
}

let overlay  = null;
const dotEls = {};

export function showFrame(map, frame, opacity) {
  const bounds = L.latLngBounds(frame.bounds[0], frame.bounds[1]);
  const src    = frame.image + '?t=' + frame.timestamp;

  // Usuń stary overlay tylko gdy zmieniają się bounds (inny radar/produkt).
  // Dzięki temu nie miga stary obraz innego radaru w nowej lokalizacji.
  // Dla animacji tego samego produktu (te same bounds) tylko URL się zmienia.
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
}

export function clearOverlay() {
  if (overlay) { overlay.remove(); overlay = null; }
}

export function setOverlayOpacity(opacity) {
  if (overlay) overlay.setOpacity(opacity);
}

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
