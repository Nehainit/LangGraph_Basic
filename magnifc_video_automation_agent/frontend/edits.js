// The Edits tab: pick a finished film, then add music, sound effects, text, caption styles, filters,
// trim & speed, and voiceover. Everything previews live from the film's separate parts (picture, narration,
// captions); Export renders the edit on the server as a new version.
import { authHeaders, goToSignIn } from './auth.js';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 2];
const TRACK_TINTS = ['#9b8cff', '#f2a541', '#3cc4b4', '#ef77b4', '#7fe3ff'];
const SWATCHES = ['#ffd23f', '#ffffff', '#141413', '#ff7a59', '#7fe3ff'];
const FORMAT_NAMES = { '9:16': 'Portrait', '16:9': 'Landscape', '1:1': 'Square' };
// Each look as CSS filter functions: [function, full value, identity, unit]; strength moves between them.
const FILTERS = {
  original: { label: 'Original', css: [['contrast', 1.04, 1], ['saturate', 1.06, 1]] },
  warm: { label: 'Warm', css: [['sepia', 0.25, 0], ['saturate', 1.2, 1], ['hue-rotate', -8, 0, 'deg']] },
  cool: { label: 'Cool', css: [['saturate', 0.95, 1], ['hue-rotate', 12, 0, 'deg'], ['brightness', 1.02, 1]] },
  vivid: { label: 'Vivid', css: [['saturate', 1.5, 1], ['contrast', 1.08, 1]] },
  mono: { label: 'B&W', css: [['grayscale', 1, 0], ['contrast', 1.1, 1]] },
  vintage: { label: 'Vintage', css: [['sepia', 0.45, 0], ['contrast', 0.9, 1], ['brightness', 1.05, 1]] },
  fade: { label: 'Fade', css: [['contrast', 0.85, 1], ['brightness', 1.1, 1], ['saturate', 0.8, 1]] },
  dusk: { label: 'Dusk', css: [['sepia', 0.2, 0], ['saturate', 1.3, 1], ['hue-rotate', -20, 0, 'deg'], ['brightness', 0.95, 1]] },
  teal: { label: 'Teal & orange', css: [['saturate', 1.25, 1], ['hue-rotate', -12, 0, 'deg'], ['contrast', 1.1, 1]] },
};
// Sizes and positions match video_automation/edit_overlays.py, so the preview is what Export draws.
const CAPTIONS = {
  pop: { label: 'Bold pop', sample: 'STEADY', size: 0.06 },
  karaoke: { label: 'Word highlight', sample: 'slow and', size: 0.054 },
  boxed: { label: 'Boxed', sample: 'Slow and', size: 0.05 },
  minimal: { label: 'Minimal', sample: 'slow and steady', size: 0.041 },
  serif: { label: 'Storybook', sample: 'Slow & steady', size: 0.054 },
  off: { label: 'Off', sample: 'No captions', size: 0 },
};
const CAPTION_Y = { top: 0.16, middle: 0.5, bottom: 0.82 };
const TEXT_SIZE = 0.032;
const TEXT_STYLES = [
  ['highlight', 'Highlight'], ['bold', 'BOLD'], ['serif', 'Serif'], ['outline', 'Outline'], ['typewriter', 'Typewriter'], ['label', 'Label'],
];
const PLAY_PATH = 'M8 5v14l11-7z';
const PAUSE_PATH = 'M6 5h4v14H6zM14 5h4v14h-4z';

function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.entries(props).forEach(([key, value]) => {
    if (value == null || value === false) return;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'style' && typeof value === 'object') Object.assign(node.style, value);
    else if (key in node && typeof value !== 'string') node[key] = value;
    else node.setAttribute(key, value);
  });
  node.append(...children.flat().filter(child => child != null && child !== false));
  return node;
}

function svg(path, className = 'ed-icon-fill') {
  const ns = 'http://www.w3.org/2000/svg';
  const icon = document.createElementNS(ns, 'svg');
  icon.setAttribute('class', className);
  icon.setAttribute('viewBox', '0 0 24 24');
  icon.setAttribute('aria-hidden', 'true');
  const shape = document.createElementNS(ns, 'path');
  shape.setAttribute('d', path);
  icon.append(shape);
  return icon;
}

const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
const pad = number => String(number).padStart(2, '0');
function clock(seconds) {
  const value = Math.max(0, seconds);
  return `${pad(Math.floor(value / 60))}:${pad(Math.floor(value % 60))}.${Math.floor((value * 10) % 10)}`;
}
const minutes = seconds => `${Math.floor(seconds / 60)}:${pad(Math.round(seconds % 60))}`;
const titleCase = tag => tag.replace(/_/g, ' ').replace(/^./, letter => letter.toUpperCase());

function filterCss(id, strength = 1) {
  return (FILTERS[id] || FILTERS.original).css
    .map(([fn, full, identity, unit = '']) => `${fn}(${(identity + (full - identity) * strength).toFixed(3)}${unit})`)
    .join(' ') || 'none';
}

function relativeDay(seconds) {
  if (!seconds) return '';
  const date = new Date(seconds * 1000);
  const today = new Date();
  const days = Math.round((new Date(today.toDateString()) - new Date(date.toDateString())) / 86400000);
  if (days === 0) return 'today';
  if (days === 1) return 'yesterday';
  return date.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}

async function api(method, url, body, { raw = false, contentType } = {}) {
  let response;
  try {
    response = await fetch(url, {
      method,
      headers: { ...(raw ? { 'Content-Type': contentType } : { 'Content-Type': 'application/json' }), ...(await authHeaders()) },
      body: body === undefined ? undefined : raw ? body : JSON.stringify(body),
    });
  } catch {
    throw new Error('Could not reach the server. Is the backend running?');
  }
  if (response.status === 401) {
    goToSignIn();
    throw new Error('Your session has expired. Sign in again to continue.');
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data?.detail;
    const message = typeof detail === 'string' ? detail
      : Array.isArray(detail) ? detail.map(item => item.msg).join(' ')
        : detail?.error || `The server returned an error (${response.status}).`;
    throw new Error(message);
  }
  return data;
}

export function setupEdits({ toast, knownFilms, onExported }) {
  const view = $('#editsView');
  const gallery = $('#edGallery');
  const editor = $('#edEditor');
  const panel = $('#edPanel');
  const timeline = $('#edTimeline');
  const frame = $('#edFrame');
  const picture = $('#edPicture');
  const narration = $('#edNarration');
  const music = $('#edMusic');
  const preview = $('#edPreview');

  const S = {
    films: [], catalog: null, threadId: null, film: null, edits: null, version: 0,
    history: [], future: [], gesture: null, saveTimer: null, saveState: 'saved',
    tool: 'audio', mood: 'all', musicQuery: '', sfxTag: 'all', pickedSfx: null, selected: null,
    playing: false, t: 0, lastT: 0, raf: 0, voiceUrls: {}, takes: new Map(), recording: null, exporting: false,
  };

  // ---------- Gallery ----------
  async function loadGallery() {
    try {
      const known = (knownFilms?.() || []).join(',');
      const data = await api('GET', `/api/edits/films${known ? `?known=${encodeURIComponent(known)}` : ''}`);
      S.films = data.films;
    } catch (error) {
      toast(error.message);
      S.films = [];
    }
    renderGallery();
  }

  function filmMeta(film) {
    const parts = [FORMAT_NAMES[film.aspect_ratio] || film.aspect_ratio];
    const finished = new Date(film.finished_at * 1000);
    parts.push(finished.toLocaleDateString(undefined, { day: 'numeric', month: 'short' }));
    return parts.join(' · ');
  }

  function poster(film, className) {
    return film.poster ? h('img', { src: film.poster, alt: '', loading: 'lazy' }) : h('span', { class: className || 'ed-no-poster' });
  }

  function renderGallery() {
    const query = $('#edFilmSearch').value.trim().toLowerCase();
    const films = S.films.filter(film => !query || film.title.toLowerCase().includes(query));
    const edited = films.filter(film => film.edited_at).sort((a, b) => b.edited_at - a.edited_at).slice(0, 3);
    $('#edContinue').hidden = !edited.length;
    $('#edContinueList').replaceChildren(...edited.map(film => {
      const summary = [
        film.added.includes('Music') && 'music',
        film.sound_cues && `${film.sound_cues} sound effect${film.sound_cues === 1 ? '' : 's'}`,
        film.added.includes('Text') && 'text',
        film.added.includes('Voiceover') && 'voiceover',
      ].filter(Boolean).join(', ');
      return h('button', { type: 'button', class: 'ed-continue-card', 'data-open-film': film.thread_id },
        poster(film),
        h('span', { class: 'ed-continue-text' },
          h('strong', { text: film.title }),
          h('span', { class: 'ed-muted', text: [film.versions ? `Version ${film.versions}` : 'Not exported yet', `edited ${relativeDay(film.edited_at)}`, summary].filter(Boolean).join(' · ') }),
          h('span', { class: 'ed-chips' }, film.added.map(kind => h('span', { class: 'ed-chip-tag', 'data-kind': kind, text: kind })))),
        h('span', { class: 'ed-open', text: 'Open editor' }));
    }));
    $('#edFilmGrid').replaceChildren(...films.map(film => h('button', { type: 'button', class: 'ed-film', 'data-open-film': film.thread_id },
      h('span', { class: 'ed-film-poster', 'data-ratio': film.aspect_ratio }, poster(film), h('span', { class: 'ed-duration', text: minutes(film.seconds) })),
      h('span', { class: 'ed-film-title', text: film.title }),
      h('span', { class: 'ed-muted', text: filmMeta(film) }))));
    $('#edFilmsEmpty').hidden = films.length > 0;
  }

  // ---------- Opening a film ----------
  async function open(threadId) {
    pause();
    try {
      const [catalog, data] = await Promise.all([S.catalog || api('GET', '/api/sounds'), api('GET', `/api/films/${encodeURIComponent(threadId)}/edits`)]);
      S.catalog = catalog;
      Object.assign(S, {
        threadId, film: data.film, edits: data.edits, version: data.version, history: [], future: [], gesture: null,
        selected: null, pickedSfx: null, t: data.edits.trim_start || 0, lastT: 0, voiceUrls: data.film.voiceover_urls || {},
        saveState: 'saved', downloadUrl: data.version ? data.video_url : null,
      });
    } catch (error) {
      toast(error.message);
      return;
    }
    S.takes.forEach(audio => audio.pause());
    S.takes.clear();
    gallery.hidden = true;
    editor.hidden = false;
    frame.dataset.ratio = S.film.aspect_ratio;
    picture.src = S.film.picture_url;
    picture.poster = S.film.shots.find(shot => shot.image_url)?.image_url || '';
    narration.src = S.film.narration_url;
    setMusicSource();
    $('#edFilmTitle').textContent = S.film.title;
    $('#edTotal').textContent = clock(S.film.seconds);
    picture.addEventListener('loadedmetadata', () => seek(S.t), { once: true });
    renderAll();
  }

  function back() {
    pause();
    if (S.recording) stopRecording();
    flushSave();
    editor.hidden = true;
    gallery.hidden = false;
    S.threadId = null;
    loadGallery();
  }

  // ---------- Edit history and autosave ----------
  const snapshot = () => JSON.stringify(S.edits);

  function commit(change) {
    S.history.push(snapshot());
    S.future = [];
    change(S.edits);
    scheduleSave();
    renderAll();
  }

  // Sliders and typing change the edit continuously; one undo step covers the whole gesture.
  function beginGesture() {
    if (!S.gesture) S.gesture = snapshot();
  }
  function endGesture() {
    if (S.gesture && S.gesture !== snapshot()) {
      S.history.push(S.gesture);
      S.future = [];
      scheduleSave();
    }
    S.gesture = null;
    renderUndo();
  }

  function undo() {
    if (!S.history.length) return;
    S.future.push(snapshot());
    S.edits = JSON.parse(S.history.pop());
    afterHistoryJump();
  }
  function redo() {
    if (!S.future.length) return;
    S.history.push(snapshot());
    S.edits = JSON.parse(S.future.pop());
    afterHistoryJump();
  }
  function afterHistoryJump() {
    S.selected = null;
    setMusicSource();
    scheduleSave();
    renderAll();
  }

  function scheduleSave() {
    S.saveState = 'saving';
    renderSaved();
    clearTimeout(S.saveTimer);
    S.saveTimer = setTimeout(save, 700);
  }
  function flushSave() {
    if (S.saveState === 'saving' && S.threadId) { clearTimeout(S.saveTimer); save(); }
  }
  async function save() {
    const threadId = S.threadId;
    if (!threadId) return;
    try {
      await api('PUT', `/api/films/${encodeURIComponent(threadId)}/edits`, S.edits);
      if (S.threadId === threadId) S.saveState = 'saved';
    } catch (error) {
      if (S.threadId === threadId) { S.saveState = 'error'; toast(error.message); }
    }
    renderSaved();
  }

  function renderSaved() {
    const label = { saving: 'Saving…', error: 'Not saved' }[S.saveState]
      || (S.version ? `Saved · version ${S.version}` : 'Saved · not exported yet');
    $('#edSaved').textContent = label;
  }
  function renderUndo() {
    $('#edUndo').disabled = !S.history.length;
    $('#edRedo').disabled = !S.future.length;
  }

  // ---------- Export ----------
  async function exportEdit() {
    if (S.exporting) return;
    pause();
    S.exporting = true;
    const button = $('#edExport');
    button.disabled = true;
    $('#edExportLabel').textContent = 'Exporting…';
    try {
      clearTimeout(S.saveTimer);
      const data = await api('POST', `/api/films/${encodeURIComponent(S.threadId)}/edits`, S.edits);
      S.version = data.version;
      S.saveState = 'saved';
      S.downloadUrl = `${data.video_url}?v=${data.version}`;
      onExported?.(S.threadId, S.downloadUrl);
      toast(`Exported version ${data.version}. Your original film is kept.`);
    } catch (error) {
      toast(error.message);
    } finally {
      S.exporting = false;
      button.disabled = false;
      $('#edExportLabel').textContent = 'Export';
      renderSaved();
      renderDownload();
    }
  }
  function renderDownload() {
    const link = $('#edDownload');
    link.hidden = !S.downloadUrl;
    if (S.downloadUrl) {
      link.href = S.downloadUrl;
      link.textContent = `Download version ${S.version}`;
    }
  }

  // ---------- Playback: the picture is the clock; every sound follows it ----------
  const trimEnd = () => S.edits.trim_end ?? S.film.seconds;
  const speaking = t => S.film.captions.some(caption => t >= caption.start && t < caption.end);
  const soundUrl = id => [...(S.catalog?.music || []), ...(S.catalog?.sfx || [])].find(item => item.id === id)?.url;
  const soundName = id => [...(S.catalog?.music || []), ...(S.catalog?.sfx || [])].find(item => item.id === id)?.name || id;
  const sound = id => [...(S.catalog?.music || []), ...(S.catalog?.sfx || [])].find(item => item.id === id);

  function setMusicSource() {
    const url = S.edits?.music_id ? soundUrl(S.edits.music_id) : '';
    if (!url) { music.pause(); music.removeAttribute('src'); music.load(); return; }
    if (!music.src.endsWith(url)) music.src = url;
  }

  function voiceSpan(t) {
    return S.edits.voiceovers.some(take => t >= take.start_seconds && t < take.start_seconds + take.seconds);
  }

  function syncMedia(t) {
    const rate = S.edits.speed;
    const narrationMuted = S.edits.mute_narration_under_voice && voiceSpan(t);
    narration.volume = narrationMuted ? 0 : clamp(S.edits.narration_volume, 0, 1);
    for (const media of [narration]) {
      media.playbackRate = rate;
      if (Math.abs(media.currentTime - t) > 0.15) media.currentTime = Math.min(t, media.duration || t);
      if (S.playing && media.paused && t < (media.duration || Infinity)) media.play().catch(() => {});
    }
    if (S.edits.music_id && music.src) {
      const duration = music.duration || 0;
      const target = duration ? t % duration : t;
      if (Math.abs(music.currentTime - target) > 0.25) music.currentTime = target;
      let level = S.edits.music_volume;
      if (S.edits.duck_music && (speaking(t) || voiceSpan(t))) level *= 0.4;
      if (S.edits.fade_music) level *= clamp(Math.min(t / 1, (S.film.seconds - t) / 1.5), 0, 1);
      music.volume = clamp(level, 0, 1);
      music.playbackRate = rate;
      if (S.playing && music.paused) music.play().catch(() => {});
    }
    S.edits.voiceovers.forEach(take => {
      const url = S.voiceUrls[take.id];
      if (!url) return;
      let audio = S.takes.get(take.id);
      if (!audio) { audio = new Audio(url); S.takes.set(take.id, audio); }
      const offset = t - take.start_seconds;
      const inside = offset >= 0 && offset < take.seconds;
      audio.playbackRate = rate;
      if (inside && S.playing) {
        if (Math.abs(audio.currentTime - offset) > 0.2) audio.currentTime = offset;
        if (audio.paused) audio.play().catch(() => {});
      } else if (!audio.paused) audio.pause();
    });
  }

  function fireEffects(previous, t) {
    if (t < previous || t - previous > 0.6) return; // a seek, not playback
    S.edits.sound_cues.forEach(cue => {
      if (cue.start_seconds > previous && cue.start_seconds <= t) {
        const url = soundUrl(cue.sound_id);
        if (!url) return;
        const effect = new Audio(url);
        effect.volume = clamp(cue.volume, 0, 1);
        effect.playbackRate = S.edits.speed;
        effect.play().catch(() => {});
      }
    });
  }

  function tick() {
    const t = picture.currentTime;
    if (t >= trimEnd() - 0.02) {
      pause();
      seek(trimEnd());
      return;
    }
    fireEffects(S.lastT, t);
    S.lastT = t;
    S.t = t;
    syncMedia(t);
    renderTime();
    S.raf = requestAnimationFrame(tick);
  }

  function play() {
    if (S.playing || !S.film) return;
    if (S.t >= trimEnd() - 0.05 || S.t < S.edits.trim_start) seek(S.edits.trim_start);
    S.playing = true;
    picture.playbackRate = S.edits.speed;
    picture.play().then(() => {
      S.lastT = picture.currentTime;
      syncMedia(picture.currentTime);
      cancelAnimationFrame(S.raf);
      S.raf = requestAnimationFrame(tick);
    }).catch(() => { S.playing = false; renderPlayButton(); });
    renderPlayButton();
  }

  function pause() {
    S.playing = false;
    cancelAnimationFrame(S.raf);
    [picture, narration, music, preview, ...S.takes.values()].forEach(media => media.pause());
    renderPlayButton();
  }

  function seek(t) {
    if (!S.film) return;
    S.t = clamp(t, 0, S.film.seconds);
    S.lastT = S.t;
    picture.currentTime = S.t;
    syncMedia(S.t);
    renderTime();
  }

  function renderPlayButton() {
    $('#edPlay').setAttribute('aria-label', S.playing ? 'Pause' : 'Play');
    $('#edPlayIcon').firstElementChild.setAttribute('d', S.playing ? PAUSE_PATH : PLAY_PATH);
  }

  // ---------- Preview overlays ----------
  function frameBox() {
    const box = frame.getBoundingClientRect();
    return { width: box.width || 1, height: box.height || 1 };
  }

  function renderCaption() {
    const layer = $('#edCaption');
    const style = S.edits.caption_style;
    const caption = style === 'off' ? null : S.film.captions.find(item => S.t >= item.start && S.t < item.end);
    if (!caption) { layer.replaceChildren(); return; }
    const { width } = frameBox();
    const words = caption.words.length ? caption.words : [{ text: caption.text, start_seconds: caption.start }];
    let active = -1;
    words.forEach((word, index) => { if (S.t >= word.start_seconds) active = index; });
    const line = h('span', { class: 'ed-caption-line' }, words.flatMap((word, index) => [
      h('span', { class: index === active ? 'is-active' : null, text: word.text }), index < words.length - 1 ? ' ' : null,
    ]));
    layer.replaceChildren(h('div', {
      class: 'ed-caption', 'data-style': style,
      style: { top: `${CAPTION_Y[S.edits.caption_position] * 100}%`, fontSize: `${width * CAPTIONS[style].size}px` },
    }, line));
  }

  function renderTexts() {
    const { height } = frameBox();
    const showAll = S.tool === 'text' && !S.playing;
    $('#edTexts').replaceChildren(...S.edits.texts
      .filter(item => (S.t >= item.start_seconds && S.t < item.end_seconds) || (showAll && S.selected?.kind === 'text' && S.selected.id === item.id))
      .map(item => h('div', {
        class: `ed-text-item${S.selected?.kind === 'text' && S.selected.id === item.id ? ' is-selected' : ''}`,
        'data-style': item.style, 'data-text-id': item.id, text: item.text || ' ',
        style: {
          left: `${item.x * 100}%`, top: `${item.y * 100}%`, fontSize: `${height * TEXT_SIZE * item.size}px`,
          color: item.style === 'highlight' ? '#141413' : item.color,
          background: item.style === 'highlight' ? item.color : '',
        },
      })));
    $$('.ed-text-item').forEach(node => node.style.setProperty('--stroke', S.edits.texts.find(item => item.id === node.dataset.textId)?.color));
  }

  function renderFilter() {
    picture.style.filter = filterCss(S.edits.filter, S.edits.filter_strength);
  }

  function renderTime() {
    $('#edNow').textContent = clock(S.t);
    renderPlayhead();
    renderCaption();
    renderTexts();
    if (!S.playing) $$('[data-at-playhead]', panel).forEach(node => { node.textContent = node.dataset.atPlayhead.replace('{t}', clock(S.t)); });
  }

  // ---------- Timeline ----------
  const pct = seconds => `${(seconds / S.film.seconds) * 100}%`;

  function lane(name, label, ...content) {
    return [
      h('span', { class: 'ed-lane-label', 'data-lane': name, text: label }),
      h('div', { class: 'ed-lane', 'data-lane-track': name }, ...content),
    ];
  }

  function renderTimeline() {
    const seconds = S.film.seconds;
    const step = seconds > 60 ? 10 : 5;
    const ticks = [];
    for (let mark = 0; mark <= seconds + 0.01; mark += step) ticks.push(h('span', { style: { left: pct(mark) }, text: `${mark}s` }));
    const isSelected = (kind, id) => S.selected?.kind === kind && S.selected.id === id;
    timeline.replaceChildren(
      h('span', { class: 'ed-lane-label' }),
      h('div', { class: 'ed-ruler', 'data-seek': '' }, ticks),
      ...lane('video', 'Video', h('div', { class: 'ed-shots', 'data-seek': '' }, S.film.shots.map(shot => (shot.image_url
        ? h('img', { src: shot.image_url, alt: '', style: { flex: `${shot.end - shot.start} 1 0` }, draggable: false })
        : h('span', { style: { flex: `${shot.end - shot.start} 1 0` } }))))),
      ...lane('text', 'Text', S.edits.texts.map(item => h('span', {
        class: `ed-block ed-block-text${isSelected('text', item.id) ? ' is-selected' : ''}`, 'data-drag': 'text', 'data-id': item.id,
        style: { left: pct(item.start_seconds), width: pct(item.end_seconds - item.start_seconds) }, text: item.text || 'Text',
      }))),
      ...lane('captions', 'Captions', S.edits.caption_style === 'off' ? [] : S.film.captions.map(caption => h('span', {
        class: 'ed-block ed-block-caption', style: { left: pct(caption.start), width: pct(caption.end - caption.start) }, text: caption.text, title: caption.text,
      }))),
      ...lane('voice', 'Voice',
        h('span', { class: 'ed-block ed-block-voice', style: { left: '0', width: '100%' } }),
        S.edits.voiceovers.map(take => h('span', {
          class: `ed-block ed-block-take${isSelected('take', take.id) ? ' is-selected' : ''}`, 'data-drag': 'take', 'data-id': take.id,
          style: { left: pct(take.start_seconds), width: pct(take.seconds) }, text: 'Your voice',
        }))),
      ...lane('music', 'Music', S.edits.music_id
        ? h('span', { class: 'ed-block ed-block-music', style: { left: '0', width: '100%' } },
          svg('M9 18V5l11-2v13M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0zm11-2a3 3 0 1 1-6 0 3 3 0 0 1 6 0z', 'ed-icon'),
          `${soundName(S.edits.music_id)} · ${titleCase(sound(S.edits.music_id)?.tag || '')}`)
        : h('span', { class: 'ed-block ed-block-music is-empty', style: { left: '0', width: '100%' }, text: 'No music — pick a song in Music' })),
      ...lane('sfx', 'Sound FX', S.edits.sound_cues.map((cue, index) => h('span', {
        class: `ed-block ed-block-sfx${isSelected('cue', index) ? ' is-selected' : ''}`, 'data-drag': 'cue', 'data-id': String(index),
        style: { left: pct(cue.start_seconds), width: pct(Math.max(0.4, sound(cue.sound_id)?.seconds || 1)) }, text: soundName(cue.sound_id),
        title: `${soundName(cue.sound_id)} at ${clock(cue.start_seconds)}`,
      }))),
    );
    timeline.append(h('div', { class: 'ed-playhead-layer', style: { position: 'absolute', left: '104px', right: '12px', top: '0', bottom: '0', pointerEvents: 'none' } },
      h('span', { class: 'ed-trim-shade', 'data-shade': 'start' }),
      h('span', { class: 'ed-trim-shade', 'data-shade': 'end' }),
      h('span', { class: 'ed-playhead' })));
    renderPlayhead();
  }

  function renderPlayhead() {
    if (!S.film) return;
    const layer = $('.ed-playhead-layer', timeline);
    if (!layer) return;
    $('.ed-playhead', layer).style.left = pct(S.t);
    const start = $('[data-shade="start"]', layer);
    const end = $('[data-shade="end"]', layer);
    start.style.left = '0';
    start.style.width = pct(S.edits.trim_start);
    end.style.left = pct(trimEnd());
    end.style.right = '0';
  }

  function timeAt(clientX) {
    const track = $('.ed-ruler', timeline).getBoundingClientRect();
    return clamp(((clientX - track.left) / track.width) * S.film.seconds, 0, S.film.seconds);
  }

  // ---------- Tool panels ----------
  const slider = (label, key, min, max, step, value, format) => h('label', { class: 'ed-slider' }, label,
    h('input', { type: 'range', min: String(min), max: String(max), step: String(step), value: String(value), 'data-edit': key, 'aria-label': label }),
    h('output', { text: format(value) }));
  const check = (label, key) => h('label', { class: 'ed-check' }, label, h('input', { type: 'checkbox', checked: S.edits[key], 'data-edit-check': key, 'aria-label': label }));
  const pills = (items, active, attribute) => h('div', { class: 'ed-pills' }, items.map(([id, label]) => h('button', {
    type: 'button', class: `ed-pill${active === id ? ' is-active' : ''}`, [attribute]: id, text: label,
  })));

  function musicPanel() {
    const tags = [...new Set(S.catalog.music.map(track => track.tag))];
    const query = S.musicQuery.toLowerCase();
    const tracks = S.catalog.music.filter(track => (S.mood === 'all' || track.tag === S.mood)
      && (!query || `${track.name} ${track.tag}`.toLowerCase().includes(query)));
    return [
      h('div', { class: 'ed-panel-head' }, h('h2', { text: 'Music' }), h('span', { class: 'ed-muted', text: 'Royalty-free library' })),
      h('label', { class: 'ed-search' },
        svg('M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zm9 16-3.5-3.5', 'ed-icon'),
        h('input', { type: 'search', placeholder: 'Search songs and moods', 'aria-label': 'Search music', value: S.musicQuery, 'data-music-search': '' })),
      pills([['all', 'All'], ...tags.map(tag => [tag, titleCase(tag)])], S.mood, 'data-mood'),
      h('div', { class: 'ed-tracks' }, tracks.length ? tracks.map((track, index) => {
        const added = S.edits.music_id === track.id;
        return h('div', { class: `ed-track${added ? ' is-selected' : ''}` },
          h('button', { type: 'button', class: 'ed-track-play', 'data-preview': track.url, 'aria-label': `Preview ${track.name}`, style: { background: TRACK_TINTS[index % TRACK_TINTS.length] } }, svg(PLAY_PATH)),
          h('div', { class: 'ed-track-text' }, h('strong', { text: track.name }), h('span', { text: `${titleCase(track.tag)} · ${minutes(track.seconds)}` })),
          h('button', { type: 'button', class: `ed-use${added ? ' is-added' : ''}`, 'data-use-music': track.id, text: added ? 'Added' : 'Use' }));
      }) : h('p', { class: 'ed-hint', text: 'No songs match. Try another mood or search.' })),
      h('div', { class: 'ed-box' },
        slider('Music', 'music_volume', 0, 1, 0.01, S.edits.music_volume, value => `${Math.round(value * 100)}%`),
        slider('Voice', 'narration_volume', 0, 2, 0.05, S.edits.narration_volume, value => `${Math.round(value * 100)}%`),
        check('Lower music while the narrator speaks', 'duck_music'),
        check('Fade in and out', 'fade_music'),
        S.edits.music_id ? h('button', { type: 'button', class: 'ed-danger', 'data-remove-music': '', text: 'Remove music' }) : null),
    ];
  }

  function sfxPanel() {
    const tags = [...new Set(S.catalog.sfx.map(effect => effect.tag))];
    const effects = S.catalog.sfx.filter(effect => S.sfxTag === 'all' || effect.tag === S.sfxTag);
    const picked = S.catalog.sfx.find(effect => effect.id === S.pickedSfx);
    const cue = S.selected?.kind === 'cue' ? S.edits.sound_cues[S.selected.id] : null;
    return [
      h('h2', { text: 'Sound effects' }),
      pills([['all', 'All'], ...tags.map(tag => [tag, titleCase(tag)])], S.sfxTag, 'data-sfx-tag'),
      h('div', { class: 'ed-sfx-grid' }, effects.map(effect => h('button', {
        type: 'button', class: `ed-sfx${S.pickedSfx === effect.id ? ' is-selected' : ''}`, 'data-pick-sfx': effect.id,
      },
      h('span', { class: 'ed-sfx-icon' }, svg(PLAY_PATH)),
      h('span', { class: 'ed-sfx-name' }, h('strong', { text: effect.name }), h('span', { text: `${titleCase(effect.tag)} · ${effect.seconds.toFixed(1)} s` }))))),
      h('button', {
        type: 'button', class: 'ed-primary', 'data-add-sfx': '', disabled: !picked,
        'data-at-playhead': picked ? `Add “${picked.name}” at {t}` : 'Pick an effect to add', text: picked ? `Add “${picked.name}” at ${clock(S.t)}` : 'Pick an effect to add',
      }),
      cue ? h('div', { class: 'ed-box' },
        h('strong', { text: `${soundName(cue.sound_id)} at ${clock(cue.start_seconds)}` }),
        slider('Volume', `cue.${S.selected.id}.volume`, 0, 1.5, 0.05, cue.volume, value => `${Math.round(value * 100)}%`),
        h('button', { type: 'button', class: 'ed-danger', 'data-remove-cue': String(S.selected.id), text: 'Remove this effect' })) : null,
      h('p', { class: 'ed-hint', text: 'Drag an effect on the timeline to move it. Click it to change its volume or remove it.' }),
    ];
  }

  function textPanel() {
    const item = S.selected?.kind === 'text' ? S.edits.texts.find(text => text.id === S.selected.id) : null;
    const activeStyle = item?.style || S.nextTextStyle || 'highlight';
    return [
      h('h2', { text: 'Text' }),
      h('button', { type: 'button', class: 'ed-dashed', 'data-add-text': '', 'data-at-playhead': '+ Add text at {t}', text: `+ Add text at ${clock(S.t)}` }),
      h('div', { class: 'ed-styles' }, TEXT_STYLES.map(([id, label]) => h('button', {
        type: 'button', class: `ed-style${activeStyle === id ? ' is-active' : ''}`, 'data-text-style': id, 'aria-label': `${label} style`,
      }, h('span', { class: `ed-style-sample ed-style-${id}`, text: label })))),
      item ? h('div', { class: 'ed-box' },
        h('label', { class: 'ed-field' }, 'Text', h('input', { type: 'text', value: item.text, maxLength: 120, 'data-text-field': 'text' })),
        h('div', { class: 'ed-swatches', role: 'group', 'aria-label': 'Text colour' }, SWATCHES.map(color => h('button', {
          type: 'button', class: `ed-swatch${item.color === color ? ' is-active' : ''}`, 'data-text-color': color, 'aria-label': `Colour ${color}`, style: { background: color },
        }))),
        slider('Size', `text.size`, 0.4, 3, 0.05, item.size, value => `${Math.round(value * 100)}%`),
        h('div', { class: 'ed-two' },
          h('label', { class: 'ed-field' }, 'Starts', h('input', { type: 'number', min: '0', max: String(S.film.seconds), step: '0.1', value: item.start_seconds.toFixed(1), 'data-text-field': 'start_seconds' })),
          h('label', { class: 'ed-field' }, 'Ends', h('input', { type: 'number', min: '0.1', max: String(S.film.seconds), step: '0.1', value: item.end_seconds.toFixed(1), 'data-text-field': 'end_seconds' }))),
        h('button', { type: 'button', class: 'ed-danger', 'data-remove-text': item.id, text: 'Delete this text' })) : null,
      h('p', { class: 'ed-hint', text: item ? 'Drag the text on the preview to place it.' : 'Select a text on the timeline or the preview to change it.' }),
    ];
  }

  function captionsPanel() {
    return [
      h('h2', { text: 'Captions' }),
      h('p', { class: 'ed-hint', text: 'Made from the narration, word by word.' }),
      h('div', { class: 'ed-caption-tiles' }, Object.entries(CAPTIONS).map(([id, style]) => h('button', {
        type: 'button', class: `ed-caption-tile${S.edits.caption_style === id ? ' is-active' : ''}`, 'data-caption-style': id,
      }, h('span', { class: `ed-caption-sample ed-caption-sample-${id}`, text: style.sample }), h('small', { text: style.label })))),
      h('div', { class: 'ed-segmented', role: 'group', 'aria-label': 'Caption position' }, ['top', 'middle', 'bottom'].map(position => h('button', {
        type: 'button', class: S.edits.caption_position === position ? 'is-active' : null, 'data-caption-position': position, text: titleCase(position),
      }))),
    ];
  }

  function filtersPanel() {
    const still = S.film.shots.find(shot => shot.image_url)?.image_url;
    return [
      h('h2', { text: 'Filters' }),
      h('div', { class: 'ed-filters' }, Object.entries(FILTERS).map(([id, look]) => h('button', {
        type: 'button', class: `ed-filter${S.edits.filter === id ? ' is-active' : ''}`, 'data-filter': id,
      }, h('span', {}, still ? h('img', { src: still, alt: '', style: { filter: filterCss(id) } }) : null), look.label))),
      slider('Strength', 'filter_strength', 0, 1, 0.01, S.edits.filter_strength, value => `${Math.round(value * 100)}%`),
    ];
  }

  function speedPanel() {
    const end = trimEnd();
    const length = (end - S.edits.trim_start) / S.edits.speed;
    return [
      h('h2', { text: 'Trim & speed' }),
      h('div', { class: 'ed-section' },
        h('strong', { text: 'Speed' }),
        h('div', { class: 'ed-segmented', role: 'group', 'aria-label': 'Playback speed' }, SPEEDS.map(speed => h('button', {
          type: 'button', class: S.edits.speed === speed ? 'is-active' : null, 'data-speed': String(speed), text: `${speed}×`,
        }))),
        h('span', { class: 'ed-muted', text: S.edits.speed === 1 ? `Original speed · ${length.toFixed(1)} s` : `New length ${length.toFixed(1)} s · the narrator’s pitch stays natural` })),
      h('div', { class: 'ed-section' },
        h('strong', { text: 'Trim the ends' }),
        h('div', { class: 'ed-trim' },
          S.film.shots.map(shot => (shot.image_url ? h('img', { src: shot.image_url, alt: '', style: { flex: `${shot.end - shot.start} 1 0` } }) : null)),
          h('span', { class: 'ed-trim-dim', style: { left: '0', width: pct(S.edits.trim_start) } }),
          h('span', { class: 'ed-trim-dim', style: { left: pct(end), right: '0' } }),
          h('span', { class: 'ed-trim-frame', style: { left: pct(S.edits.trim_start), width: pct(end - S.edits.trim_start) } })),
        slider('Starts', 'trim_start', 0, S.film.seconds, 0.1, S.edits.trim_start, value => clock(value)),
        slider('Ends', 'trim_end', 0, S.film.seconds, 0.1, end, value => clock(value))),
    ];
  }

  function voicePanel() {
    const recording = S.recording;
    return [
      h('h2', { text: 'Voiceover' }),
      h('p', { class: 'ed-hint', text: 'Record over the film from the playhead. The film plays while you speak; headphones keep the narrator out of your recording.' }),
      h('button', {
        type: 'button', class: `ed-record${recording?.state === 'recording' ? ' is-recording' : ''}`, 'data-record': '',
        'aria-label': recording ? 'Stop recording' : 'Start recording',
      }, recording?.state === 'countdown' ? String(recording.count)
        : recording ? svg('M7 7h10v10H7z', 'ed-icon-fill')
          : svg('M9 3h6v11H9zM5 11a7 7 0 0 0 14 0M12 18v3', 'ed-icon')),
      h('span', {
        class: 'ed-center', 'data-at-playhead': recording ? '' : 'Starts at {t} after a 3-second count',
        text: recording?.state === 'recording' ? 'Recording… press stop when you’re done' : recording ? 'Get ready…' : `Starts at ${clock(S.t)} after a 3-second count`,
      }),
      h('div', { class: 'ed-box' },
        check('Mute the AI narrator while I speak', 'mute_narration_under_voice'),
        check('Clean up background noise', 'clean_voice')),
      S.edits.voiceovers.length ? h('ul', { class: 'ed-takes' }, S.edits.voiceovers.map((take, index) => h('li', {},
        h('span', { text: `Take ${index + 1} · ${clock(take.start_seconds)} – ${clock(take.start_seconds + take.seconds)}` }),
        h('button', { type: 'button', 'data-remove-take': take.id, text: 'Delete' })))) : null,
    ];
  }

  const PANELS = { audio: musicPanel, sfx: sfxPanel, text: textPanel, captions: captionsPanel, filters: filtersPanel, speed: speedPanel, voice: voicePanel };

  function renderPanel() {
    const scroll = panel.scrollTop;
    panel.replaceChildren(...PANELS[S.tool]().filter(Boolean));
    panel.scrollTop = scroll;
  }

  function renderRail() {
    $$('[data-ed-tool]').forEach(button => button.classList.toggle('is-active', button.dataset.edTool === S.tool));
  }

  function renderAll() {
    if (!S.film) return;
    renderRail();
    renderPanel();
    renderTimeline();
    renderFilter();
    renderTime();
    renderSaved();
    renderUndo();
    renderDownload();
    renderPlayButton();
  }

  // ---------- Previewing sounds ----------
  function togglePreview(button) {
    const playing = button.classList.contains('is-playing');
    preview.pause();
    $$('.is-playing', view).forEach(item => item.classList.remove('is-playing'));
    if (playing) return;
    preview.src = button.dataset.preview;
    preview.play().catch(() => {});
    button.classList.add('is-playing');
  }
  preview.addEventListener('ended', () => $$('.is-playing', view).forEach(item => item.classList.remove('is-playing')));

  // ---------- Voiceover recording ----------
  async function startRecording() {
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    } catch {
      toast('Allow microphone access in your browser to record a voiceover.');
      return;
    }
    pause();
    const start = S.t;
    S.recording = { state: 'countdown', count: 3, stream, start };
    renderPanel();
    for (const count of [3, 2, 1]) {
      if (!S.recording) return;
      S.recording.count = count;
      renderPanel();
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    if (!S.recording) return;
    const type = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'].find(item => window.MediaRecorder?.isTypeSupported?.(item)) || '';
    const recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
    const chunks = [];
    recorder.addEventListener('dataavailable', event => { if (event.data.size) chunks.push(event.data); });
    recorder.addEventListener('stop', () => uploadTake(new Blob(chunks, { type: recorder.mimeType || 'audio/webm' }), start));
    Object.assign(S.recording, { state: 'recording', recorder });
    recorder.start();
    renderPanel();
    seek(start);
    play();
  }

  function stopRecording() {
    const recording = S.recording;
    if (!recording) return;
    S.recording = null;
    pause();
    if (recording.recorder?.state === 'recording') recording.recorder.stop();
    recording.stream.getTracks().forEach(track => track.stop());
    renderPanel();
  }

  async function uploadTake(blob, start) {
    const threadId = S.threadId;
    try {
      const data = await api('POST', `/api/films/${encodeURIComponent(threadId)}/voiceovers`, blob, { raw: true, contentType: blob.type.split(';')[0] || 'audio/webm' });
      if (S.threadId !== threadId) return;
      S.voiceUrls[data.id] = data.url;
      commit(edits => edits.voiceovers.push({ id: data.id, start_seconds: Math.round(start * 10) / 10, seconds: Math.min(data.seconds, S.film.seconds - start) }));
      toast('Voiceover added.');
    } catch (error) {
      toast(error.message);
    }
  }

  // ---------- Events ----------
  $('#edFilmSearch').addEventListener('input', renderGallery);
  gallery.addEventListener('click', event => {
    const film = event.target.closest('[data-open-film]');
    if (film) open(film.dataset.openFilm);
  });
  $('#edBack').addEventListener('click', back);
  $('#edUndo').addEventListener('click', undo);
  $('#edRedo').addEventListener('click', redo);
  $('#edExport').addEventListener('click', exportEdit);
  $('#edPlay').addEventListener('click', () => (S.playing ? pause() : play()));
  $('#edBackFive').addEventListener('click', () => seek(S.t - 5));
  $('#edForwardFive').addEventListener('click', () => seek(S.t + 5));
  $$('[data-ed-tool]').forEach(button => button.addEventListener('click', () => {
    S.tool = button.dataset.edTool;
    renderRail();
    renderPanel();
    renderTexts();
  }));

  panel.addEventListener('click', event => {
    const target = event.target.closest('button');
    if (!target) return;
    const data = target.dataset;
    if ('preview' in data) { togglePreview(target); return; }
    if ('mood' in data) { S.mood = data.mood; renderPanel(); return; }
    if ('sfxTag' in data) { S.sfxTag = data.sfxTag; renderPanel(); return; }
    if ('useMusic' in data) {
      commit(edits => { edits.music_id = edits.music_id === data.useMusic ? null : data.useMusic; });
      setMusicSource();
      return;
    }
    if ('removeMusic' in data) { commit(edits => { edits.music_id = null; }); setMusicSource(); return; }
    if ('pickSfx' in data) {
      S.pickedSfx = data.pickSfx;
      renderPanel();
      const effect = S.catalog.sfx.find(item => item.id === data.pickSfx);
      if (effect) { preview.src = effect.url; preview.play().catch(() => {}); }
      return;
    }
    if ('addSfx' in data && S.pickedSfx) {
      const at = Math.round(Math.min(S.t, S.film.seconds - 0.1) * 10) / 10;
      commit(edits => { edits.sound_cues.push({ sound_id: S.pickedSfx, start_seconds: at, volume: 0.6 }); });
      S.selected = { kind: 'cue', id: S.edits.sound_cues.length - 1 };
      renderAll();
      return;
    }
    if ('removeCue' in data) { S.selected = null; commit(edits => { edits.sound_cues.splice(Number(data.removeCue), 1); }); return; }
    if ('addText' in data) {
      const id = Math.random().toString(16).slice(2, 10);
      const start = Math.round(Math.min(S.t, S.film.seconds - 1) * 10) / 10;
      const style = S.nextTextStyle || 'highlight';
      commit(edits => edits.texts.push({
        id, text: 'Your text', style, color: style === 'highlight' ? '#ffd23f' : '#ffffff', x: 0.5, y: 0.15, size: 1,
        start_seconds: start, end_seconds: Math.min(S.film.seconds, start + 3),
      }));
      S.selected = { kind: 'text', id };
      renderAll();
      const field = $('[data-text-field="text"]', panel);
      field?.focus();
      field?.select();
      return;
    }
    if ('textStyle' in data) {
      S.nextTextStyle = data.textStyle;
      const item = S.selected?.kind === 'text' && S.edits.texts.find(text => text.id === S.selected.id);
      if (item) commit(() => { item.style = data.textStyle; }); else renderPanel();
      return;
    }
    if ('textColor' in data) {
      const item = S.edits.texts.find(text => text.id === S.selected?.id);
      if (item) commit(() => { item.color = data.textColor; });
      return;
    }
    if ('removeText' in data) { S.selected = null; commit(edits => { edits.texts = edits.texts.filter(item => item.id !== data.removeText); }); return; }
    if ('captionStyle' in data) { commit(edits => { edits.caption_style = data.captionStyle; }); return; }
    if ('captionPosition' in data) { commit(edits => { edits.caption_position = data.captionPosition; }); return; }
    if ('filter' in data) { commit(edits => { edits.filter = data.filter; }); return; }
    if ('speed' in data) { commit(edits => { edits.speed = Number(data.speed); }); return; }
    if ('record' in data) { if (S.recording) stopRecording(); else startRecording(); return; }
    if ('removeTake' in data) {
      S.takes.get(data.removeTake)?.pause();
      commit(edits => { edits.voiceovers = edits.voiceovers.filter(take => take.id !== data.removeTake); });
    }
  });

  panel.addEventListener('pointerdown', beginGesture);
  panel.addEventListener('focusin', event => { if (event.target.matches('input')) beginGesture(); });
  panel.addEventListener('input', event => {
    const target = event.target;
    if ('musicSearch' in target.dataset) {
      S.musicQuery = target.value;
      const caret = target.selectionStart;
      renderPanel();
      const field = $('[data-music-search]', panel);
      field.focus();
      field.setSelectionRange(caret, caret);
      return;
    }
    beginGesture();
    const key = target.dataset.edit;
    if (key) {
      const value = Number(target.value);
      if (key.startsWith('cue.')) S.edits.sound_cues[Number(key.split('.')[1])].volume = value;
      else if (key === 'text.size') { const item = S.edits.texts.find(text => text.id === S.selected?.id); if (item) item.size = value; }
      else if (key === 'trim_start') S.edits.trim_start = Math.min(value, trimEnd() - 1);
      else if (key === 'trim_end') S.edits.trim_end = Math.max(value, S.edits.trim_start + 1);
      else S.edits[key] = value;
      const output = target.parentElement.querySelector('output');
      if (output) {
        const shown = key === 'trim_start' ? S.edits.trim_start : key === 'trim_end' ? trimEnd() : value;
        output.textContent = key.startsWith('trim') ? clock(shown) : `${Math.round(shown * 100)}%`;
      }
      if (key === 'filter_strength') renderFilter();
      if (key === 'text.size') renderTexts();
      if (key.startsWith('trim')) { renderPlayhead(); if (S.t < S.edits.trim_start || S.t > trimEnd()) seek(clamp(S.t, S.edits.trim_start, trimEnd())); }
      if (!S.playing) syncMedia(S.t);
      return;
    }
    const field = target.dataset.textField;
    const item = S.edits.texts.find(text => text.id === S.selected?.id);
    if (field && item) {
      if (field === 'text') item.text = target.value;
      else if (target.value !== '') item[field] = clamp(Number(target.value), 0, S.film.seconds);
      if (item.end_seconds <= item.start_seconds) item.end_seconds = Math.min(S.film.seconds, item.start_seconds + 0.5);
      renderTexts();
      renderTimeline();
    }
  });
  panel.addEventListener('change', event => {
    const key = event.target.dataset.editCheck;
    if (key) { beginGesture(); S.edits[key] = event.target.checked; }
    endGesture();
    // The trim strip and the new length follow the sliders once they settle.
    if (event.target.dataset.edit?.startsWith('trim')) renderPanel();
  });
  panel.addEventListener('focusout', event => { if (event.target.matches('input')) endGesture(); });

  // Dragging blocks on the timeline, or scrubbing the playhead.
  timeline.addEventListener('pointerdown', event => {
    if (!S.film) return;
    const block = event.target.closest('[data-drag]');
    if (!block) {
      if (!event.target.closest('.ed-lane, .ed-ruler')) return;
      pause();
      seek(timeAt(event.clientX));
      const move = moveEvent => seek(timeAt(moveEvent.clientX));
      const up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); };
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
      return;
    }
    event.preventDefault();
    const kind = block.dataset.drag;
    const id = kind === 'cue' ? Number(block.dataset.id) : block.dataset.id;
    const item = kind === 'cue' ? S.edits.sound_cues[id] : kind === 'text' ? S.edits.texts.find(text => text.id === id) : S.edits.voiceovers.find(take => take.id === id);
    if (!item) return;
    const startX = event.clientX;
    const origin = item.start_seconds;
    const length = kind === 'text' ? item.end_seconds - item.start_seconds : kind === 'take' ? item.seconds : 0.1;
    const track = $('.ed-ruler', timeline).getBoundingClientRect();
    let moved = false;
    beginGesture();
    const move = moveEvent => {
      const delta = ((moveEvent.clientX - startX) / track.width) * S.film.seconds;
      if (Math.abs(moveEvent.clientX - startX) > 3) moved = true;
      const start = Math.round(clamp(origin + delta, 0, S.film.seconds - length) * 10) / 10;
      item.start_seconds = start;
      if (kind === 'text') item.end_seconds = Math.round((start + length) * 10) / 10;
      block.style.left = pct(start);
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      S.selected = { kind, id };
      S.tool = { cue: 'sfx', text: 'text', take: 'voice' }[kind];
      endGesture();
      renderAll();
      if (!moved && kind !== 'take') seek(item.start_seconds);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  });

  // Dragging text on the preview places it.
  $('#edTexts').addEventListener('pointerdown', event => {
    const node = event.target.closest('[data-text-id]');
    if (!node) return;
    event.preventDefault();
    const item = S.edits.texts.find(text => text.id === node.dataset.textId);
    if (!item) return;
    pause();
    S.selected = { kind: 'text', id: item.id };
    S.tool = 'text';
    renderRail();
    renderPanel();
    renderTimeline();
    beginGesture();
    const box = frame.getBoundingClientRect();
    const move = moveEvent => {
      item.x = clamp((moveEvent.clientX - box.left) / box.width, 0.05, 0.95);
      item.y = clamp((moveEvent.clientY - box.top) / box.height, 0.05, 0.95);
      node.style.left = `${item.x * 100}%`;
      node.style.top = `${item.y * 100}%`;
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      endGesture();
      renderTexts();
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  });

  picture.addEventListener('ended', pause);
  new ResizeObserver(() => { if (S.film && !editor.hidden) { renderCaption(); renderTexts(); } }).observe(frame);
  window.addEventListener('beforeunload', flushSave);

  return {
    // Opening the tab shows the gallery, unless a film is already open in the editor.
    show() {
      if (!S.threadId) { gallery.hidden = false; editor.hidden = true; loadGallery(); }
    },
    hide() {
      pause();
      if (S.recording) stopRecording();
      flushSave();
    },
    open,
  };
}
