/**
 * lightning_forecast.js — renderowanie prognozy ruchu wyładowań.
 *
 * Dane (klastry, polygony, statystyki) są obliczane w Pythonie
 * i dostarczane przez lightning.json w polu "forecast.clusters".
 */

let _layer   = null;
let _visible = true;

export function initForecastLayer(map) {
  _layer = L.layerGroup().addTo(map);
}

export function getForecastVisible() { return _visible; }

export function setForecastVisible(v) {
  _visible = v;
  if (!v && _layer) _layer.clearLayers();
}

export function updateForecast(data) {
  if (!_layer) return;
  _layer.clearLayers();
  if (!_visible) return;

  const clusters = data?.forecast?.clusters;
  if (!clusters?.length) return;

  for (const cl of clusters) {
    if (!cl.polygon?.length) continue;

    const intense = cl.cluster_type === 'intense';
    L.polygon(cl.polygon, {
      color:       intense ? '#ff2200' : '#ff8800',
      weight:      intense ? 2.5       : 2,
      dashArray:   '8 5',
      opacity:     0.9,
      fillColor:   intense ? '#cc0000' : '#ff6600',
      fillOpacity: intense ? 0.18      : 0.12,
    })
    .bindPopup(_buildPopup(cl), { maxWidth: 260 })
    .addTo(_layer);
  }
}

function _fmt(v, unit, decimals = 0) {
  return v != null ? `${v.toFixed(decimals)} ${unit}` : '—';
}

function _buildPopup(cl) {
  const s   = cl.stats ?? {};
  const spd = cl.speed_kmh > 0 ? `${cl.speed_kmh} km/h` : 'brak danych';
  const dir = cl.speed_kmh > 0 ? `${cl.direction_deg}° (${cl.direction_compass})` : '—';
  const motionLabel = cl.motion_label ?? (cl.motion_source === 'gfs' ? 'GFS' : 'historia wyładowań');

  const intense = cl.cluster_type === 'intense';
  const label   = intense
    ? '<b style="color:#ff2200">⚠ Intensywna komórka burzowa +1h</b>'
    : '<b>Prognoza komórki burzowej +1h</b>';

  const hasEnv = s.cape_jkg != null || s.shear06_ms != null || s.wmaxshear != null;
  const envTime = s.env_valid_time
    ? `<tr><td colspan="2" class="fc-env-time">Środowisko GFS: ${s.env_valid_time} UTC</td></tr>` : '';
  const capeRow = s.cape_jkg != null
    ? `<tr><td>CAPE</td><td>${_fmt(s.cape_jkg, 'J/kg')}</td></tr>` : '';
  const shearRow = s.shear06_ms != null
    ? `<tr><td>Shear 0–6 km</td><td>${_fmt(s.shear06_ms, 'm/s', 1)} (${(s.shear06_ms * 1.944).toFixed(0)} kt)</td></tr>` : '';
  const wmsRow = s.wmaxshear != null
    ? `<tr><td>WmaxShear</td><td>${_fmt(s.wmaxshear, 'm²/s²')}</td></tr>` : '';
  const envSep = hasEnv ? '<tr><td colspan="2" class="fc-sep"></td></tr>' : '';

  return `<div class="lgtn-popup">
    ${label}
    <table>
      <tr><td>Prędkość</td><td>${spd}</td></tr>
      <tr><td>Kierunek</td><td>${dir}</td></tr>
      <tr><td>Wektor ruchu</td><td class="fc-source">${motionLabel}</td></tr>
      <tr><td colspan="2" class="fc-sep"></td></tr>
      <tr><td>Wyładowania (10 min)</td><td>${s.count_10min ?? '—'}</td></tr>
      <tr><td>Pole klastra</td><td>${s.area_km2 != null ? s.area_km2 + ' km²' : '—'}</td></tr>
      <tr><td>Gęstość</td><td>${s.density_km2 != null ? s.density_km2 + ' /km²' : '—'}</td></tr>
      <tr><td>Maks. gęstość</td><td>${s.max_density_km2 != null ? s.max_density_km2 + ' /km²' : '—'}</td></tr>
      ${envSep}
      ${envTime}${capeRow}${shearRow}${wmsRow}
    </table>
  </div>`;
}
