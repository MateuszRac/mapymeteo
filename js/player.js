import { PLAY_MS, MAX_FRAMES } from './config.js?v=21';

export function createPlayer({ onFrame, onClear }) {
  let frames   = [];
  let frameIdx = 0;
  let timer    = null;
  let playing  = false;
  let playMs   = PLAY_MS;

  const slider  = document.getElementById('time-slider');
  const btnPlay = document.getElementById('btn-play');

  function setSliderFill(idx, total) {
    if (!slider) return;
    const pct = total <= 1 ? (total === 1 ? 100 : 0) : Math.round((idx / (total - 1)) * 100);
    slider.style.background =
      `linear-gradient(to right, var(--accent) ${pct}%, var(--border) ${pct}%)`;
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
    frames   = (newFrames ?? []).slice(-MAX_FRAMES);
    frameIdx = frames.length - 1;
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
    const updated      = (newFrames ?? []).slice(-MAX_FRAMES);
    if (!updated.length) return 0;
    const curStamp     = frames[frameIdx]?.timestamp;
    const oldLastStamp = frames[frames.length - 1]?.timestamp;
    const wasAtLast    = frameIdx === frames.length - 1;
    frames = updated;
    if (slider) slider.max = Math.max(0, frames.length - 1);
    const newLastStamp = frames[frames.length - 1]?.timestamp;
    const hasNewFrame  = newLastStamp !== oldLastStamp;
    if (wasAtLast && hasNewFrame) {
      frameIdx = frames.length - 1;
      if (slider) slider.value = frameIdx;
      setSliderFill(frameIdx, frames.length);
      render();
    } else {
      const restored = curStamp ? frames.findIndex(f => f.timestamp === curStamp) : -1;
      frameIdx = restored >= 0 ? restored : frames.length - 1;
      if (slider) slider.value = frameIdx;
      setSliderFill(frameIdx, frames.length);
      if (!playing) render();
    }
    return hasNewFrame ? 1 : 0;
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
    get frameCount() { return frames.length; },
    get isPlaying()  { return playing; },
  };
}
