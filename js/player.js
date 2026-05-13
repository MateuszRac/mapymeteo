import { PLAY_MS, MAX_FRAMES } from './config.js?v=22';

export function createPlayer({ onFrame, onClear }) {
  let frames        = [];
  let frameIdx      = 0;
  let forecastStart = -1;   // index pierwszej klatki prognozy (-1 = brak)
  let timer         = null;
  let playing       = false;
  let playMs        = PLAY_MS;

  const slider  = document.getElementById('time-slider');
  const btnPlay = document.getElementById('btn-play');

  function setSliderFill(idx, total) {
    if (!slider) return;
    const pct  = total <= 1 ? (total === 1 ? 100 : 0) : Math.round((idx  / (total - 1)) * 100);
    const hasFc = forecastStart > 0 && forecastStart < total;
    const fPct  = hasFc ? Math.round((forecastStart / (total - 1)) * 100) : 100;
    const FC    = '#e07020';
    const FC_DIM = 'rgba(224,112,32,.28)';

    if (!hasFc) {
      slider.style.background =
        `linear-gradient(to right, var(--accent) ${pct}%, var(--border) ${pct}%)`;
    } else if (idx < forecastStart) {
      // w strefie danych rzeczywistych — prognoza widoczna jako pasek
      slider.style.background =
        `linear-gradient(to right, var(--accent) ${pct}%, var(--border) ${pct}%, var(--border) ${fPct}%, ${FC_DIM} ${fPct}%)`;
    } else {
      // w strefie prognozy
      slider.style.background =
        `linear-gradient(to right, var(--accent) ${fPct}%, ${FC} ${fPct}%, ${FC} ${pct}%, ${FC_DIM} ${pct}%)`;
    }
  }

  function render() {
    if (!frames.length) return;
    const f = frames[frameIdx];
    if (slider) slider.value = frameIdx;
    setSliderFill(frameIdx, frames.length);
    onFrame?.(f);
  }

  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
    playing = false;
    if (btnPlay) { btnPlay.textContent = '▶'; btnPlay.classList.remove('playing'); }
  }

  function play() {
    if (!frames.length) return;
    playing = true;
    if (btnPlay) { btnPlay.textContent = '⏹'; btnPlay.classList.add('playing'); }
    timer = setInterval(() => {
      frameIdx = (frameIdx + 1) % frames.length;
      render();
    }, playMs);
  }

  function setSpeed(ms) {
    playMs = ms;
    if (playing) { stop(); play(); }
  }

  function loadFrames(newFrames) {
    const was = playing;
    stop();
    frames        = (newFrames ?? []).slice(-MAX_FRAMES);
    forecastStart = frames.findIndex(f => f.is_forecast);
    // Domyślnie stajemy na ostatniej prawdziwej klatce, nie na prognozie
    frameIdx = forecastStart > 0 ? forecastStart - 1 : frames.length - 1;
    if (slider) { slider.min = 0; slider.max = Math.max(0, frames.length - 1); slider.value = frameIdx; }
    setSliderFill(frameIdx, frames.length);
    if (frames.length) {
      render();
      if (was) play();
    } else {
      setSliderFill(0, 0);
      onClear?.();
    }
  }

  function updateFrames(newFrames) {
    const updated = (newFrames ?? []).slice(-MAX_FRAMES);
    if (!updated.length) return 0;
    const curStamp = frames[frameIdx]?.timestamp;

    // Zapamiętaj pozycję ostatniej prawdziwej klatki (przed prognozą)
    const oldFcStart      = frames.findIndex(f => f.is_forecast);
    const oldLastRealIdx  = oldFcStart > 0 ? oldFcStart - 1 : frames.length - 1;
    const oldLastRealStamp = frames[oldLastRealIdx]?.timestamp;
    const wasAtLastReal    = frameIdx === oldLastRealIdx;

    frames        = updated;
    forecastStart = frames.findIndex(f => f.is_forecast);
    if (slider) slider.max = Math.max(0, frames.length - 1);

    const newLastRealIdx   = forecastStart > 0 ? forecastStart - 1 : frames.length - 1;
    const newLastRealStamp = frames[newLastRealIdx]?.timestamp;
    const hasNewRealFrame  = newLastRealStamp !== oldLastRealStamp;

    if (wasAtLastReal && hasNewRealFrame) {
      // Przesuń na nową ostatnią prawdziwą klatkę
      frameIdx = newLastRealIdx;
      if (slider) slider.value = frameIdx;
      setSliderFill(frameIdx, frames.length);
      render();
    } else {
      const restored = curStamp ? frames.findIndex(f => f.timestamp === curStamp) : -1;
      frameIdx = restored >= 0 ? restored : newLastRealIdx;
      if (slider) slider.value = frameIdx;
      setSliderFill(frameIdx, frames.length);
      if (!playing) render();
    }
    return hasNewRealFrame ? 1 : 0;
  }

  btnPlay?.addEventListener('click', () => playing ? stop() : play());

  slider?.addEventListener('input', () => {
    stop();
    frameIdx = +slider.value;
    setSliderFill(frameIdx, frames.length);
    render();
  });

  document.addEventListener('keydown', e => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    if (e.key === 'ArrowRight') { stop(); frameIdx = Math.min(frames.length - 1, frameIdx + 1); setSliderFill(frameIdx, frames.length); render(); }
    if (e.key === 'ArrowLeft')  { stop(); frameIdx = Math.max(0, frameIdx - 1); setSliderFill(frameIdx, frames.length); render(); }
    if (e.key === ' ')          { playing ? stop() : play(); e.preventDefault(); }
  });

  return {
    loadFrames,
    updateFrames,
    stop,
    play,
    setSpeed,
    get frameCount()    { return frames.length; },
    get isPlaying()     { return playing; },
    get currentFrame()  { return frames[frameIdx] ?? null; },
  };
}
