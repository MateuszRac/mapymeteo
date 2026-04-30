import { PLAY_MS, MAX_FRAMES } from './config.js';

export function createPlayer({ onFrame, onClear }) {
  let frames   = [];
  let frameIdx = 0;
  let timer    = null;
  let playing  = false;
  let playMs   = PLAY_MS;

  const slider    = document.getElementById('time-slider');
  const timeLabel = document.getElementById('time-label');
  const countEl   = document.getElementById('frame-count');
  const btnPlay   = document.getElementById('btn-play');
  const btnPrev   = document.getElementById('btn-prev');
  const btnNext   = document.getElementById('btn-next');

  function render() {
    if (!frames.length) return;
    const f = frames[frameIdx];
    slider.value         = frameIdx;
    timeLabel.textContent = f.timestamp.replace('T', ' ') + ' UTC';
    countEl.textContent   = `${frameIdx + 1} / ${frames.length}`;
    onFrame?.(f);
  }

  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
    playing = false;
    btnPlay.textContent = '▶ Play';
    btnPlay.classList.remove('playing');
  }

  function play() {
    if (!frames.length) return;
    playing = true;
    btnPlay.textContent = '⏹ Stop';
    btnPlay.classList.add('playing');
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
    slider.min   = 0;
    slider.max   = Math.max(0, frames.length - 1);
    slider.value = frameIdx;

    if (frames.length) {
      render();
      if (was) play();
    } else {
      timeLabel.textContent = '—';
      countEl.textContent   = '';
      onClear?.();
    }
  }

  btnPlay.addEventListener('click', () => playing ? stop() : play());
  btnPrev.addEventListener('click', () => {
    stop(); frameIdx = Math.max(0, frameIdx - 1); render();
  });
  btnNext.addEventListener('click', () => {
    stop(); frameIdx = Math.min(frames.length - 1, frameIdx + 1); render();
  });
  slider.addEventListener('input', () => {
    stop(); frameIdx = +slider.value; render();
  });
  document.addEventListener('keydown', e => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    if (e.key === 'ArrowRight') { stop(); frameIdx = Math.min(frames.length - 1, frameIdx + 1); render(); }
    if (e.key === 'ArrowLeft')  { stop(); frameIdx = Math.max(0, frameIdx - 1); render(); }
    if (e.key === ' ')          { playing ? stop() : play(); e.preventDefault(); }
  });

  return {
    loadFrames,
    stop,
    play,
    setSpeed,
    get frameCount() { return frames.length; },
    get isPlaying()  { return playing; },
  };
}
