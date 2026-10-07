const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const KEYS = { draft: 'studio.draft.v2', films: 'studio.films.v1', run: 'studio.run.v2' };
const store = {
  get(key, fallback) {
    try { const value = localStorage.getItem(key); return value ? JSON.parse(value) : fallback; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage unavailable */ }
  },
  remove(key) {
    try { localStorage.removeItem(key); } catch { /* storage unavailable */ }
  },
};

const STARTERS = {
  fable: 'A rabbit challenges a quiet turtle to a race through the forest. Slow and steady wins the day.',
  product: 'A 30-second reel for a reusable steel water bottle. Open on a sunrise hike, follow the bottle through a busy city day, and end on a calm rooftop at dusk. Bright, energetic, clean product shots.',
  explainer: 'Explain how bees make honey to curious eight-year-olds. Follow one worker bee from flower to hive, showing nectar, wax cells, and fanning wings. Playful, colourful, simple narration.',
};

const STORY_TEMPLATE = 'Main character: \nWhere it happens: \nWhat goes wrong: \nHow it ends: \nMood: ';
const FORMAT_NAMES = { '9:16': 'Portrait', '16:9': 'Landscape', '1:1': 'Square' };
const PLATFORMS = { magnific: 'Magnific', fal: 'fal.ai' };

const ACTION_LABELS = {
  approve: 'Looks great, continue',
  regenerate: 'Make a new version',
  retry: 'Try this step again',
  revise_narration: 'Rework the narration',
  revise_scenes: 'Rework the scenes',
  revise_visuals: 'Rework the visuals',
};

const REVIEWS = {
  story_review: { endpoint: '/api/review-story', title: 'Read your story', note: true },
  reference_board_review: { endpoint: '/api/review-reference-board', title: 'Meet the cast & mood', note: true },
  director_plan_review: { endpoint: '/api/review-director-plan', title: 'Check the shot list', note: false },
  visual_storyboard_review: { endpoint: '/api/review-visual-storyboard', title: 'Review your storyboard', note: false },
  scene_plan_review: { endpoint: '/api/review-scene-plan', title: 'The scene plan needs you', note: true },
  visual_plan_review: { endpoint: '/api/review-visual-plan', title: 'The visual plan needs you', note: true },
  shot_plan_review: { endpoint: '/api/review-shot-plan', title: 'The shot plan needs you', note: true },
};

const FAILURES = {
  judge_retry_exhausted: 'The quality critics kept rejecting some shots, even after the allowed retries.',
  video_validation_failed: 'Some animated shots failed validation after the allowed retries.',
  pipeline_incomplete: 'The pipeline stopped before a final film was ready.',
};

// ---------- Elements ----------
const form = $('#composeForm');
const topic = $('#topic');
const topicError = $('#topicError');
const duration = $('#duration');
const language = $('#language');
const narrationModel = $('#narrationModel');
const imageModel = $('#imageModel');
const videoModel = $('#videoModel');
const submitButton = $('#submitButton');
const runCard = $('#runCard');
const runPanel = $('#runPanel');
const panes = { review: $('#reviewPane'), result: $('#resultPane'), failure: $('#failurePane') };
const toastEl = $('#toast');

let catalog = [];
let run = null; // { thread_id, request, status, response, artifacts }
let shotNotes = new Map(); // shot_id -> note for the storyboard under review

// ---------- Helpers ----------
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.entries(props).forEach(([key, value]) => {
    if (value == null || value === false) return;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key in node && typeof value !== 'string') node[key] = value;
    else node.setAttribute(key, value);
  });
  node.append(...children.filter(child => child != null && child !== false));
  return node;
}

function toast(message) {
  toastEl.textContent = message;
  toastEl.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { toastEl.hidden = true; }, 3800);
}

function formatRate(cost, quality) {
  if (!cost) return '';
  const amount = cost.quality_rates?.[quality] ?? cost.amount;
  const value = cost.unit === 'USD' ? `$${amount}` : `${amount} ${cost.unit}`;
  return `${value} / ${cost.billing_unit}`;
}

function errorText(data, fallback) {
  const detail = data?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map(item => item.msg).filter(Boolean).join(' ') || fallback;
  if (detail?.error) {
    const categories = detail.categories?.length ? ` (${detail.categories.join(', ')})` : '';
    return `${detail.error}${categories}`;
  }
  return data?.error || fallback;
}

async function post(url, body) {
  let response;
  try {
    response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  } catch {
    throw new Error('Could not reach the server. Is the backend running?');
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(errorText(data, `The server returned an error (${response.status}).`));
  return data;
}

const pad = number => String(number).padStart(2, '0');

function issueText(issue) {
  if (typeof issue === 'string') return issue;
  return issue?.message || issue?.issue || issue?.description || issue?.reason || JSON.stringify(issue);
}

// ---------- Views ----------
function showView(name) {
  $('#createView').hidden = name !== 'create';
  $('#filmsView').hidden = name !== 'films';
  $$('.tab').forEach(tab => {
    const active = tab.dataset.view === name;
    tab.classList.toggle('is-active', active);
    if (active) tab.setAttribute('aria-current', 'page'); else tab.removeAttribute('aria-current');
  });
  if (name === 'films') renderFilms();
}

// ---------- Compose form ----------
function formValues() {
  const data = new FormData(form);
  const values = {
    topic: topic.value.trim(),
    duration: Number(duration.value),
    language: language.value,
    aspect_ratio: data.get('aspect_ratio'),
    visual_style: data.get('visual_style'),
    video_quality: data.get('video_quality'),
    quality_mode: data.get('quality_mode'),
    image_provider: data.get('image_provider'),
  };
  [['narration_model', narrationModel], ['image_model', imageModel], ['video_model', videoModel]].forEach(([key, select]) => {
    if (select.value) values[key] = select.value;
  });
  return values;
}

function setRadio(name, value) {
  const input = $$(`input[name="${name}"]`, form).find(item => item.value === value);
  if (input) input.checked = true;
}

function fillSelect(select, models, quality, { withRate }) {
  const previous = select.value;
  select.replaceChildren(...models.map(model => el('option', {
    value: model.id,
    text: withRate ? `${model.display_name} · ${formatRate(model.cost, quality)}` : model.display_name,
  })));
  if (models.some(model => model.id === previous)) select.value = previous;
}

// Image and video models must come from the chosen platform; the backend routes by model.
function syncModels() {
  if (!catalog.length) return;
  const data = new FormData(form);
  const provider = PLATFORMS[data.get('image_provider')];
  const quality = data.get('video_quality');
  fillSelect(imageModel, catalog.filter(model => model.stage === 'image' && model.provider === provider), quality, { withRate: true });
  fillSelect(videoModel, catalog.filter(model => model.stage === 'video' && model.provider === provider), quality, { withRate: true });
}

function syncForm() {
  syncModels();
  const values = formValues();
  $('#charCount').textContent = `${topic.value.length}/2000`;
  $('#durationOut').textContent = `${values.duration} sec`;
  duration.style.setProperty('--fill', `${((values.duration - duration.min) / (duration.max - duration.min)) * 100}%`);
  $('#summary').textContent = `${values.duration} sec · ${values.language} · ${FORMAT_NAMES[values.aspect_ratio]}`;
  $$('[data-starter]').forEach(chip => chip.classList.toggle('is-active', topic.value.trim() === STARTERS[chip.dataset.starter]));
}

function applyValues(values = {}) {
  if (typeof values.topic === 'string') topic.value = values.topic;
  if (values.duration) duration.value = values.duration;
  if (values.language) language.value = values.language;
  ['aspect_ratio', 'visual_style', 'video_quality', 'quality_mode', 'image_provider'].forEach(name => {
    if (values[name]) setRadio(name, values[name]);
  });
  syncModels();
  [['narration_model', narrationModel], ['image_model', imageModel], ['video_model', videoModel]].forEach(([key, select]) => {
    if (values[key] && $$('option', select).some(option => option.value === values[key])) select.value = values[key];
  });
  syncForm();
}

function saveDraft() {
  store.set(KEYS.draft, formValues());
}

async function loadCatalog() {
  try {
    const response = await fetch('/api/model-catalog');
    if (!response.ok) throw new Error();
    const data = await response.json();
    catalog = data.models || [];
    $('#pricingNote').textContent = data.pricing_note || '';
  } catch {
    catalog = [];
  }
  const voices = catalog.filter(model => model.stage === 'narration');
  if (voices.length) fillSelect(narrationModel, voices, null, { withRate: false });
  else narrationModel.closest('.select').hidden = true;
  if (!catalog.length) $$('.adv-row').slice(3).forEach(row => { row.hidden = true; });
  syncModels();
}

// ---------- Run views ----------
function showPane(state, { eyebrow, title } = {}) {
  form.hidden = true;
  runCard.hidden = false;
  $('#workingPane').hidden = state !== 'working';
  runPanel.hidden = state === 'working';
  Object.entries(panes).forEach(([name, pane]) => { pane.hidden = name !== state; });
  if (state !== 'working') {
    $('#runEyebrow').textContent = eyebrow;
    $('#runTitle').textContent = title;
  }
  showView('create');
  runCard.focus({ preventScroll: true });
  $('.main').scrollTo({ top: 0 });
}

function renderBrief(request) {
  $('#runBrief').replaceChildren(
    document.createTextNode(request.topic.length > 220 ? `${request.topic.slice(0, 220)}…` : request.topic),
    el('strong', { text: `${PLATFORMS[request.image_provider] || 'fal.ai'} · ${FORMAT_NAMES[request.aspect_ratio]} · ${request.duration} sec · ${request.language}` }),
  );
}

function persistRun() {
  if (!run) return store.remove(KEYS.run);
  store.set(KEYS.run, run);
}

async function startFilm(event) {
  event.preventDefault();
  const request = formValues();
  if (!request.topic) {
    topicError.textContent = 'Write a sentence or two about your story first.';
    topicError.hidden = false;
    topic.focus();
    return;
  }
  topicError.hidden = true;
  submitButton.disabled = true;
  run = { request, status: 'working', artifacts: {} };
  persistRun();
  renderBrief(request);
  showPane('working');
  try {
    handleResponse(await post('/api/create-video', request));
  } catch (error) {
    showFailure(error.message);
  } finally {
    submitButton.disabled = false;
  }
}

function handleResponse(data) {
  run = { ...run, thread_id: data.thread_id || run.thread_id, status: data.status, response: data, artifacts: { ...run.artifacts, ...(data.artifacts || {}) } };
  if (REVIEWS[data.status]) {
    persistRun();
    renderReview(data);
    return;
  }
  const videoUrl = data.video_url || run.artifacts.video_url;
  if (videoUrl) {
    run.status = 'complete';
    saveFilm(videoUrl);
    store.remove(KEYS.run);
    renderResult(videoUrl);
    return;
  }
  showFailure(data.message || FAILURES[data.status] || 'The pipeline finished without producing a film.');
}

function showFailure(message) {
  if (run) run.status = 'failed';
  store.remove(KEYS.run);
  const warnings = run?.artifacts?.warnings || [];
  $('#failureMessage').textContent = warnings.length ? `${message} ${warnings.join(' ')}` : message;
  showPane('failure', { eyebrow: 'Cut!', title: 'This take needs another try' });
}

// ---------- Carousel ----------
// Cover-flow: the active card sits in front and its neighbours fan out behind it.
function carousel(items, { ratio, label, hint, card, caption, onChange }) {
  let index = 0;
  const stage = el('div', { class: 'cf-stage', tabindex: '0', 'aria-roledescription': 'carousel', 'aria-label': label });
  const cards = items.map((item, i) => {
    const node = el('div', { class: 'cf-card', role: 'group', 'aria-roledescription': 'slide', 'aria-label': `${i + 1} of ${items.length}` }, ...card(item, i));
    node.addEventListener('click', () => { if (i !== index) go(i); });
    return node;
  });
  stage.append(...cards);
  const prev = el('button', { type: 'button', class: 'cf-nav prev', 'aria-label': 'Previous shot' });
  const next = el('button', { type: 'button', class: 'cf-nav next', 'aria-label': 'Next shot' });
  const dots = items.map((_, i) => el('button', { type: 'button', class: 'cf-dot', 'aria-label': `Go to shot ${i + 1}` }));
  const counter = el('span', { class: 'cf-count' });
  const captionBox = el('div', { class: 'cf-caption' });
  const root = el('div', { class: 'cf', 'data-ratio': ratio },
    el('div', { class: 'cf-viewport' }, prev, stage, next),
    captionBox,
    el('div', { class: 'cf-meta' }, counter, el('div', { class: 'cf-dots' }, ...dots), hint ? el('span', { class: 'cf-hint', text: hint }) : null),
  );

  function go(target) {
    index = Math.max(0, Math.min(items.length - 1, target));
    cards.forEach((node, i) => {
      const offset = i - index;
      const distance = Math.min(Math.abs(offset), 4);
      // Each step back moves less, so far cards tuck in behind near ones.
      const shift = Math.sign(offset) * 185 * (1 - 0.6 ** distance);
      node.style.transform = `translateX(${shift}%) scale(${1 - 0.11 * Math.min(distance, 3)})`;
      node.style.zIndex = String(10 - distance);
      node.style.opacity = distance > 3 ? '0' : String(1 - 0.2 * distance);
      node.style.filter = distance ? `blur(${Math.min(distance, 3) * 1.5}px)` : '';
      node.classList.toggle('is-active', offset === 0);
      node.setAttribute('aria-hidden', String(offset !== 0));
    });
    dots.forEach((dot, i) => dot.classList.toggle('is-active', i === index));
    counter.textContent = `${pad(index + 1)} / ${pad(items.length)}`;
    prev.disabled = index === 0;
    next.disabled = index === items.length - 1;
    captionBox.replaceChildren(...caption(items[index], index));
    onChange?.(index, cards);
  }

  prev.addEventListener('click', () => go(index - 1));
  next.addEventListener('click', () => go(index + 1));
  dots.forEach((dot, i) => dot.addEventListener('click', () => go(i)));
  stage.addEventListener('keydown', event => {
    if (event.key === 'ArrowLeft') go(index - 1);
    if (event.key === 'ArrowRight') go(index + 1);
  });
  let swipeStart = null;
  stage.addEventListener('pointerdown', event => { swipeStart = event.clientX; });
  stage.addEventListener('pointerup', event => {
    const distance = swipeStart == null ? 0 : event.clientX - swipeStart;
    swipeStart = null;
    if (Math.abs(distance) > 40) go(index + (distance < 0 ? 1 : -1));
  });
  go(0);
  return root;
}

function shotDetail(shot) {
  return [shot.camera, [shot.motion, shot.subject_motion].filter(Boolean).join(', ')].filter(Boolean).join(' · ');
}

function shotCaption(shot, index) {
  const line = shot?.narration || shot?.visual_action || shot?.visuals || '';
  return [
    el('p', { class: 'cf-title' }, el('strong', { text: `Shot ${pad(index + 1)}` }), line ? el('span', { text: line }) : null),
    shot && shotDetail(shot) ? el('p', { class: 'cf-detail', text: shotDetail(shot) }) : null,
  ];
}

// Image QA advises the reviewer; it never blocks the storyboard.
function qaFindings(result) {
  if (!result) return [];
  if (result.qa_error) return ['The automatic check could not review this frame.'];
  return result.approved === false ? (result.issues || []).map(issue => issue.description).filter(Boolean) : [];
}

// Plans give times either as seconds (3.2) or as clock text ("00:03.21").
function shotTime(shot) {
  const format = value => String(value).includes(':') ? String(value).replace(/^00:/, '0:') : `${value}s`;
  return `${format(shot.start_time)} – ${format(shot.end_time)}`;
}

function storyboardCarousel(shots, images, qaResults) {
  const qaById = new Map(qaResults.map(result => [result.shot_id, result]));
  const items = shots.map((shot, index) => {
    const id = shot.shot_id || `shot-${pad(index + 1)}`;
    return { shot, image: images[index], id, findings: qaFindings(qaById.get(id)) };
  });
  return carousel(items, {
    ratio: run.request.aspect_ratio,
    label: 'Storyboard',
    hint: 'Leave a note on any shot you want redrawn',
    card: (item, index) => [
      item.image ? el('img', { src: item.image, alt: `Frame for shot ${index + 1}`, draggable: 'false' }) : el('span', { class: 'cf-empty', text: 'No frame' }),
      el('span', { class: 'cf-tag', text: `Shot ${pad(index + 1)}` }),
      item.shot.start_time != null ? el('span', { class: 'cf-time', text: shotTime(item.shot) }) : null,
      el('span', { class: 'cf-pill', text: 'Note', hidden: !shotNotes.get(item.id) }),
      item.findings.length ? el('span', { class: 'cf-pill cf-warn', text: 'Check' }) : null,
      el('span', { class: 'cf-pending', text: 'Animating…' }),
    ],
    caption: (item, index) => {
      const note = el('textarea', { class: 'shot-note', rows: '2', 'aria-label': `Changes for shot ${index + 1}`, placeholder: 'What should change in this shot? Leave blank to keep it.' });
      note.value = shotNotes.get(item.id) || '';
      note.addEventListener('input', () => {
        if (note.value.trim()) shotNotes.set(item.id, note.value); else shotNotes.delete(item.id);
        $$('.cf-card .cf-pill', $('#reviewBody'))[index].hidden = !shotNotes.has(item.id);
        syncRedoButton();
      });
      const findings = item.findings.length
        ? el('ul', { class: 'cf-qa', 'aria-label': 'Automatic check' }, ...item.findings.map(text => el('li', { text })))
        : null;
      return [...shotCaption(item.shot, index), findings, $('#renderingBar').hidden ? note : null];
    },
  });
}

function clipCarousel(clips, storyboard) {
  const shots = new Map(storyboard.map((shot, index) => [shot.shot_id, { shot, index }]));
  return carousel(clips, {
    ratio: run.request.aspect_ratio,
    label: 'Your clips',
    hint: 'Click the clip in front to pause',
    card: (clip, index) => {
      const video = el('video', { src: clip.video_url, poster: clip.image_url || '', muted: true, loop: true, playsInline: true, preload: 'metadata' });
      video.addEventListener('click', () => {
        if (!video.closest('.is-active')) return;
        if (video.paused) video.play().catch(() => {}); else video.pause();
      });
      return [video, el('span', { class: 'cf-tag', text: `Shot ${pad((shots.get(clip.shot_id)?.index ?? index) + 1)}` })];
    },
    caption: (clip, index) => {
      const match = shots.get(clip.shot_id);
      return shotCaption(match?.shot, match?.index ?? index);
    },
    onChange: (index, cards) => cards.forEach((card, i) => {
      const video = $('video', card);
      if (i === index) video.play().catch(() => {}); else video.pause();
    }),
  });
}

function syncRedoButton() {
  const button = $('[data-review-action="regenerate"]', $('#reviewActions'));
  if (!button || run?.status !== 'visual_storyboard_review') return;
  const count = shotNotes.size;
  button.textContent = count ? `Redraw ${count} noted shot${count === 1 ? '' : 's'}` : 'Redraw shots with notes';
  button.disabled = !count;
}

// ---------- Reviews ----------
function shotFields(shot) {
  return [
    ['Purpose', shot.story_purpose],
    ['Visuals', shot.visuals],
    ['Camera', shot.camera],
    ['Motion', [shot.motion, shot.subject_motion].filter(Boolean).join(' · ')],
    ['Narration', shot.narration],
    ['Characters', (shot.characters_present || []).join(', ')],
  ].filter(([, value]) => value);
}

function shotCard(shot, index, { image, editable }) {
  const shotId = shot.shot_id || `shot-${String(index + 1).padStart(3, '0')}`;
  const timing = shot.start_time != null ? ` · ${shot.start_time}–${shot.end_time}` : '';
  return el('article', { class: 'shot' },
    image ? el('img', { src: image, alt: `Frame for ${shotId}`, loading: 'lazy' }) : null,
    el('h4', { text: `${shotId}${timing}` }),
    ...shotFields(shot).map(([label, value]) => el('p', {}, el('strong', { text: `${label}: ` }), value)),
    editable ? el('textarea', { class: 'shot-note', rows: '2', 'data-shot-id': shotId, 'aria-label': `Changes for ${shotId}`, placeholder: 'Leave blank to keep this shot' }) : null,
  );
}

function renderBible(bible = {}) {
  const list = value => Array.isArray(value) ? value.join(', ') : value;
  const characters = Array.isArray(bible.characters) ? bible.characters.map(item => `${item.name}: ${item.appearance}`).join(' · ') : '';
  const rows = [
    ['Theme', bible.theme], ['Visual style', bible.visual_style], ['Palette', list(bible.palette)],
    ['Lighting', bible.lighting], ['Camera', bible.camera_language], ['Locations', list(bible.locations)], ['Characters', characters],
  ].filter(([, value]) => value);
  if (!rows.length) return null;
  return el('dl', { class: 'bible' }, ...rows.flatMap(([label, value]) => [el('dt', { text: label }), el('dd', { text: value })]));
}

function renderReview(data) {
  const config = REVIEWS[data.status];
  const artifacts = run.artifacts;
  const reason = $('#reviewReason');
  const issues = $('#reviewIssues');
  $('#reviewError').hidden = true;
  $('#reviewNote').value = '';
  $('#renderingBar').hidden = true;
  $('#reviewFooter').hidden = false;

  const redrawn = Array.isArray(data.feedback) ? data.feedback.length : 0;
  reason.textContent = data.revision_reason || data.count_adjustment
    || (typeof data.feedback === 'string' && data.feedback ? `Applied your notes: ${data.feedback}` : '')
    || (redrawn ? `Redrew ${redrawn} shot${redrawn === 1 ? '' : 's'} from your notes.` : '');
  reason.hidden = !reason.textContent;
  issues.replaceChildren(...(data.issues || []).map(issue => el('li', { text: issueText(issue) })));
  issues.hidden = !issues.children.length;

  let content = null;
  if (data.status === 'story_review') {
    content = el('textarea', { class: 'story-edit', id: 'storyEdit', 'aria-label': 'Your story, editable' });
    content.value = data.story || artifacts.story || '';
    content.dataset.original = content.value;
  } else if (data.status === 'reference_board_review') {
    content = el('div', {},
      el('div', { class: 'boards' },
        el('figure', {}, el('img', { src: data.character_board_url || artifacts.character_board_url, alt: 'Character board' }), el('figcaption', { text: 'Characters' })),
        el('figure', {}, el('img', { src: data.mood_board_url || artifacts.mood_board_url, alt: 'Mood board' }), el('figcaption', { text: 'Mood' })),
      ),
      renderBible(data.production_bible || artifacts.production_bible),
    );
  } else if (data.status === 'director_plan_review') {
    const shots = data.director_plan || artifacts.director_plan || [];
    content = el('div', { class: 'shots' }, ...shots.map((shot, index) => shotCard(shot, index, { editable: true })));
  } else if (data.status === 'visual_storyboard_review') {
    shotNotes = new Map();
    const qaResults = data.shot_image_qa_results || artifacts.shot_image_qa_results || [];
    content = storyboardCarousel(data.storyboard || artifacts.storyboard || [], data.image_urls || artifacts.image_urls || [], qaResults);
    const flagged = qaResults.filter(result => qaFindings(result).length).length;
    if (flagged && !reason.textContent) {
      reason.textContent = `Our automatic check flagged ${flagged} shot${flagged === 1 ? '' : 's'} marked “Check”. Take a closer look, then animate or leave a note to redraw.`;
      reason.hidden = false;
    }
  }
  $('#reviewBody').replaceChildren(...(content ? [content] : []));

  $('#noteField').hidden = !config.note;
  $('#noteHint').textContent = data.status.endsWith('_plan_review') ? '(optional)' : '(needed if you ask for a new version)';

  const actions = (data.actions || ['approve', 'regenerate']).filter(action => action !== 'edit');
  const storyboard = data.status === 'visual_storyboard_review';
  $('#reviewActions').replaceChildren(...actions.map((action, index) => el('button', {
    type: 'button',
    class: index === 0 ? 'primary-button compact' : 'secondary-button',
    'data-review-action': action,
    text: storyboard && action === 'approve' ? 'Animate these shots'
      : action === 'regenerate' && !config.note ? 'Redo the shots I noted'
      : (ACTION_LABELS[action] || action.replaceAll('_', ' ')),
  })));
  syncRedoButton();
  showPane('review', { eyebrow: 'Your call', title: config.title });
}

// The storyboard stays on screen while its shots are animated.
function showRendering() {
  const count = $$('#reviewBody .cf-card').length;
  run.busy = true;
  $('#reviewBody .cf')?.classList.add('is-rendering');
  $('#reviewBody .shot-note')?.remove();
  $('#reviewReason').hidden = true;
  $('#reviewFooter').hidden = true;
  $('#renderingText').textContent = `Animating ${count} shot${count === 1 ? '' : 's'} and cutting your film. This usually takes a few minutes.`;
  $('#renderingBar').hidden = false;
  $('#runEyebrow').textContent = 'Action!';
  $('#runTitle').textContent = 'Animating your storyboard';
}

async function submitReview(action) {
  const status = run.status;
  const config = REVIEWS[status];
  const errorBox = $('#reviewError');
  const note = $('#reviewNote').value.trim();
  const body = { thread_id: run.thread_id, action };
  const fail = message => { errorBox.textContent = message; errorBox.hidden = false; };

  if (status === 'story_review') {
    const story = $('#storyEdit');
    if (action === 'approve' && story.value.trim() !== story.dataset.original.trim()) {
      if (!story.value.trim()) return fail('The story can’t be empty.');
      body.action = 'edit';
      body.story = story.value;
    }
  }
  if (action === 'regenerate' && config.note) {
    if (!note) return fail('Describe what should change first.');
    body.note = note;
  } else if (action === 'regenerate' && status === 'visual_storyboard_review') {
    body.feedback = [...shotNotes].map(([shot_id, text]) => ({ shot_id, note: text.trim() })).filter(item => item.note);
    if (!body.feedback.length) return fail('Add a note to at least one shot you want redrawn.');
  } else if (action === 'regenerate') {
    body.feedback = $$('.shot-note').map(input => ({ shot_id: input.dataset.shotId, note: input.value.trim() })).filter(item => item.note);
    if (!body.feedback.length) return fail('Add a note to at least one shot you want redone.');
  } else if (config.note && note && status.endsWith('_plan_review')) {
    body.note = note;
  }

  errorBox.hidden = true;
  if (status === 'visual_storyboard_review' && action === 'approve') showRendering();
  else showPane('working');
  try {
    handleResponse(await post(config.endpoint, body));
  } catch (error) {
    renderReview(run.response);
    fail(error.message);
  } finally {
    if (run) run.busy = false;
  }
}

// ---------- Result ----------
function noteText(note) {
  const lead = String(note).split(/: (?:POST|GET) https?:\/\//)[0];
  return lead.length > 160 ? `${lead.slice(0, 157)}…` : lead;
}

function renderResult(videoUrl) {
  const artifacts = run.artifacts;
  const video = $('#resultVideo');
  video.src = videoUrl;
  video.poster = artifacts.image_urls?.find(Boolean) || '';
  $('#downloadLink').href = videoUrl;
  const notes = artifacts.warnings || [];
  $('#resultWarnings').hidden = !notes.length;
  $('#resultWarningsSummary').textContent = `${notes.length} production note${notes.length === 1 ? '' : 's'}`;
  // Provider errors carry raw JSON; keep the readable lead-in and leave the full text on hover.
  $('#resultWarningsList').replaceChildren(...notes.map(note => el('li', { text: noteText(note), title: note })));
  $('#resultStory').textContent = artifacts.story || '';
  $('#storyDetails').hidden = !artifacts.story;
  const clips = (artifacts.clips || []).filter(clip => clip.video_url)
    .sort((a, b) => String(a.shot_id).localeCompare(String(b.shot_id), undefined, { numeric: true }));
  $('#clipsSection').hidden = !clips.length;
  $('#clipCarousel').replaceChildren(...(clips.length ? [clipCarousel(clips, artifacts.storyboard || [])] : []));
  showPane('result', { eyebrow: 'That’s a wrap', title: 'Your film is ready' });
}

function backToForm({ keepPrompt }) {
  $('#resultVideo').pause();
  $$('#clipCarousel video').forEach(video => video.pause());
  run = null;
  persistRun();
  runCard.hidden = true;
  form.hidden = false;
  if (!keepPrompt) { topic.value = ''; saveDraft(); }
  syncForm();
  showView('create');
  topic.focus();
}

// ---------- My films ----------
function films() {
  return store.get(KEYS.films, []);
}

function saveFilm(videoUrl) {
  const list = films().filter(film => film.thread_id !== run.thread_id);
  list.unshift({
    thread_id: run.thread_id,
    video_url: videoUrl,
    poster: run.artifacts.image_urls?.find(Boolean) || null,
    prompt: run.request.topic,
    aspect_ratio: run.request.aspect_ratio,
    duration: run.request.duration,
    created_at: Date.now(),
  });
  store.set(KEYS.films, list.slice(0, 48));
}

function renderFilms() {
  const list = films();
  $('#filmsEmpty').hidden = list.length > 0;
  $('#filmsGrid').replaceChildren(...list.map(film => el('article', { class: 'film' },
    el('button', { type: 'button', class: 'film-open', 'data-video': film.video_url, 'aria-label': `Play: ${film.prompt}` },
      el('span', { class: 'film-thumb' }, film.poster ? el('img', { src: film.poster, alt: '', loading: 'lazy' }) : '▶')),
    el('p', { class: 'film-prompt', text: film.prompt }),
    el('div', { class: 'film-meta' },
      el('span', { text: `${new Date(film.created_at).toLocaleDateString()} · ${FORMAT_NAMES[film.aspect_ratio] || film.aspect_ratio} · ${film.duration} sec` }),
      el('button', { type: 'button', 'data-remove': film.thread_id, text: 'Remove' })),
  )));
}

const dialog = $('#playerDialog');
const dialogVideo = $('#playerVideo');
dialog.addEventListener('close', () => { dialogVideo.pause(); dialogVideo.removeAttribute('src'); dialogVideo.load(); });
dialog.addEventListener('click', event => {
  if (event.target === dialog || event.target.closest('[data-close]')) dialog.close();
});

// ---------- Events ----------
form.addEventListener('submit', startFilm);
form.addEventListener('input', () => { syncForm(); saveDraft(); topicError.hidden = true; });
form.addEventListener('change', () => { syncForm(); saveDraft(); });
topic.addEventListener('keydown', event => {
  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) form.requestSubmit();
});

$$('[data-starter]').forEach(button => button.addEventListener('click', () => {
  topic.value = STARTERS[button.dataset.starter];
  syncForm();
  saveDraft();
  topic.focus();
}));

$('#helpWrite').addEventListener('click', () => {
  if (topic.value.trim()) {
    toast('Tip: say who it’s about, where it happens, what goes wrong, and how it ends.');
  } else {
    topic.value = STORY_TEMPLATE;
    syncForm();
    saveDraft();
  }
  topic.focus();
  const firstGap = topic.value.indexOf(': \n');
  if (firstGap >= 0 && topic.value.startsWith(STORY_TEMPLATE.slice(0, 15))) topic.setSelectionRange(firstGap + 2, firstGap + 2);
});

$('#reviewActions').addEventListener('click', event => {
  const button = event.target.closest('[data-review-action]');
  if (button) submitReview(button.dataset.reviewAction);
});

runCard.addEventListener('click', event => {
  const action = event.target.closest('[data-action]')?.dataset.action;
  if (action === 'remix' || action === 'edit') backToForm({ keepPrompt: true });
  if (action === 'fresh') backToForm({ keepPrompt: false });
});

document.addEventListener('click', event => {
  const viewButton = event.target.closest('[data-view]');
  if (viewButton) { showView(viewButton.dataset.view); return; }
  const player = event.target.closest('[data-video]');
  if (player) {
    dialogVideo.src = player.dataset.video;
    dialog.showModal();
    dialogVideo.play().catch(() => {});
    return;
  }
  const remove = event.target.closest('[data-remove]');
  if (remove) {
    store.set(KEYS.films, films().filter(film => film.thread_id !== remove.dataset.remove));
    renderFilms();
    toast('Removed from this browser. The file stays on the server.');
  }
});

window.addEventListener('beforeunload', event => {
  if (run?.status === 'working' || run?.busy) event.preventDefault();
});

// ---------- Boot ----------
(async function boot() {
  await loadCatalog();
  // Every session starts on fal.ai (Magnific is only the backend fallback), so the platform is not restored.
  const { image_provider: _provider, image_model: _image, video_model: _video, ...draft } = store.get(KEYS.draft, {});
  applyValues(draft);

  const saved = store.get(KEYS.run, null);
  if (saved?.status && REVIEWS[saved.status] && saved.response) {
    run = { ...saved, busy: false };
    renderBrief(run.request);
    renderReview(run.response);
  } else if (saved?.status === 'working') {
    store.remove(KEYS.run);
    toast('Your last film was interrupted when the page closed. Your story is still here.');
  }
})();
