import { authHeaders, ensureAccess, goToSignIn, loadClerk } from './auth.js';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const DRAFT_KEY = 'characters.draft.v1';
const RETURN_KEY = 'characters.returnTo'; // set by the story writer when it sends someone here to create a character
const STYLE_NAMES = { realistic: 'Realistic', cinematic: 'Cinematic', illustrated: 'Illustrated', '3d_animation': '3D', anime: 'Anime' };
const FIELDS = ['name', 'kind', 'appearance', 'outfit', 'props'];
const MAX_REFERENCES = 3;
const REFERENCE_SIDE = 1600;
const BOARDS = {
  sheet: { card: '#sheetCard', label: 'character sheet', busy: 'Drawing the character sheet…' },
  emotions: { card: '#emotionsCard', label: 'emotion board', busy: 'Drawing the emotion board…' },
};

const form = $('#charForm');
const toastEl = $('#toast');

let library = { characters: [], emotions: ['happy', 'sad', 'angry', 'surprised', 'scared', 'thinking'], image_cost_usd: 0.15 };
let current = null; // the character open in the editor; null while creating a new one
const drawing = new Map(); // character id -> 'sheet' | 'emotions'
const errors = new Map(); // `${id}:${board}` -> message
let pendingRefs = []; // data URLs picked for a character that is not saved yet

// ---------- Helpers ----------
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.entries(props).forEach(([key, value]) => {
    if (value == null || value === false) return;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
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

function storage(action, value) {
  try {
    if (action === 'get') return JSON.parse(localStorage.getItem(DRAFT_KEY) || 'null');
    if (action === 'set') localStorage.setItem(DRAFT_KEY, JSON.stringify(value));
    if (action === 'remove') localStorage.removeItem(DRAFT_KEY);
  } catch { /* storage unavailable */ }
  return null;
}

function errorText(data, fallback) {
  const detail = data?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map(item => item.msg).filter(Boolean).join(' ') || fallback;
  return detail?.error || fallback;
}

async function api(method, url, body) {
  let response;
  try {
    response = await fetch(url, {
      method,
      headers: { ...(body ? { 'Content-Type': 'application/json' } : {}), ...(await authHeaders()) },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new Error('Could not reach the server. Is the backend running?');
  }
  if (response.status === 401) {
    goToSignIn();
    throw new Error('Your session has expired. Sign in again to continue.');
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(errorText(data, `The server returned an error (${response.status}).`));
  return data;
}

const usd = amount => `$${amount.toFixed(2)}`;

function upsert(record) {
  library.characters = [record, ...library.characters.filter(item => item.id !== record.id)];
  if (current?.id === record.id) current = record;
}

// ---------- Routing ----------
function route() {
  const id = location.hash.slice(1);
  if (!id) return showLibrary();
  if (id === 'new') return showEditor(null);
  const record = library.characters.find(item => item.id === id);
  if (!record) {
    toast('That character is no longer in your library.');
    history.replaceState(null, '', location.pathname);
    return showLibrary();
  }
  return showEditor(record);
}

function returnTo() {
  try { return JSON.parse(localStorage.getItem(RETURN_KEY) || 'null'); } catch { return null; }
}

function forgetReturn() {
  try { localStorage.removeItem(RETURN_KEY); } catch { /* storage unavailable */ }
}

function renderReturnNote() {
  const back = returnTo();
  const note = $('#returnNote');
  note.hidden = !back;
  if (!back) return;
  const ready = current?.sheet && !drawing.has(current.id);
  note.replaceChildren(
    document.createTextNode(ready
      ? `${current.name} is ready. Go back and swap them into your story. `
      : `You’re creating ${back.name} for your story. Once they’re drawn, go back and swap them in. `),
    el('a', { class: 'link-button', href: back.href, 'data-return': '', text: 'Back to the story writer' }),
  );
}

function showLibrary() {
  // Leaving the editor for the library means the story hand-off was abandoned.
  forgetReturn();
  current = null;
  $('#libraryView').hidden = false;
  $('#editorView').hidden = true;
  renderLibrary();
}

function showEditor(record) {
  current = record;
  if (record) pendingRefs = [];
  $('#libraryView').hidden = true;
  $('#editorView').hidden = false;
  fillForm(record || storage('get') || {});
  $('#formError').hidden = true;
  $$('[aria-invalid]', form).forEach(input => input.removeAttribute('aria-invalid'));
  renderEditor();
  $('.main').scrollTo({ top: 0 });
  if (!record) $('#name').focus();
}

// ---------- Library ----------
function renderLibrary() {
  const list = library.characters;
  $('#charEmpty').hidden = list.length > 0;
  $('#charGrid').replaceChildren(...list.map(record => el('a', { class: 'char-card', href: `#${record.id}` },
    el('span', { class: 'char-thumb' },
      record.sheet_url || record.references?.[0]
        ? el('img', { src: record.sheet_url || record.references[0].url, alt: '', loading: 'lazy' })
        : el('span', { class: 'initials', 'aria-hidden': 'true', text: record.name.trim().slice(0, 1).toUpperCase() }),
      el('span', { class: 'char-pill', text: drawing.has(record.id) ? 'Drawing…' : STYLE_NAMES[record.visual_style] })),
    el('p', { class: 'char-name', text: record.name }),
    el('p', { class: 'char-kind', text: record.kind }),
  )));
}

// ---------- Editor ----------
function fillForm(values) {
  FIELDS.forEach(name => { form.elements[name].value = values[name] || ''; });
  const style = $$('input[name="visual_style"]', form).find(input => input.value === (values.visual_style || 'illustrated'));
  if (style) style.checked = true;
}

function formDetails() {
  const details = Object.fromEntries(FIELDS.map(name => [name, form.elements[name].value.trim()]));
  details.visual_style = new FormData(form).get('visual_style') || 'illustrated';
  return details;
}

// ---------- Reference images ----------
function referenceItems() {
  return current ? current.references || [] : pendingRefs.map((url, index) => ({ name: String(index), url }));
}

function renderRefs(busy) {
  const items = referenceItems();
  const nodes = items.map((item, index) => el('figure', { class: 'ref' },
    el('img', { src: item.url, alt: `Reference image ${index + 1}` }),
    el('button', { class: 'ref-remove', type: 'button', 'data-remove-ref': item.name, 'aria-label': `Remove reference image ${index + 1}`, disabled: busy, text: '×' })));
  if (items.length < MAX_REFERENCES) {
    nodes.push(el('label', { class: `ref-add${busy ? ' is-disabled' : ''}` },
      el('input', { class: 'visually-hidden', type: 'file', accept: 'image/png,image/jpeg,image/webp', multiple: true, disabled: busy, 'data-ref-input': '' }),
      el('span', { 'aria-hidden': 'true', text: '+' }),
      el('span', { text: 'Add image' })));
  }
  $('#refs').replaceChildren(...nodes);
}

// Shrinking in the browser keeps uploads small; drawing on white flattens transparency the same way the server does.
async function shrink(file) {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, REFERENCE_SIDE / Math.max(bitmap.width, bitmap.height));
  const canvas = Object.assign(document.createElement('canvas'), { width: Math.round(bitmap.width * scale), height: Math.round(bitmap.height * scale) });
  const context = canvas.getContext('2d');
  context.fillStyle = '#fff';
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  return canvas.toDataURL('image/jpeg', 0.9);
}

async function addReferences(files) {
  const room = MAX_REFERENCES - referenceItems().length;
  const images = [...files].filter(file => file.type.startsWith('image/'));
  if (images.length > room) toast(`A character can have ${MAX_REFERENCES} reference images, so only the first ${room} were added.`);
  for (const file of images.slice(0, Math.max(room, 0))) {
    let image;
    try {
      image = await shrink(file);
    } catch {
      toast(`Couldn’t read ${file.name}. Try a JPG or PNG.`);
      continue;
    }
    if (!current) {
      pendingRefs.push(image);
      continue;
    }
    try {
      upsert(await api('POST', `/api/characters/${current.id}/references`, { image }));
    } catch (error) {
      toast(error.message);
    }
  }
  renderEditor();
}

async function removeReference(name) {
  if (!current) {
    pendingRefs.splice(Number(name), 1);
    renderEditor();
    return;
  }
  try {
    upsert(await api('DELETE', `/api/characters/${current.id}/references/${name}`));
  } catch (error) {
    toast(error.message);
  }
  renderEditor();
}

function boardFrame(record, board) {
  const frame = $(`${BOARDS[board].card} .board-frame`);
  const url = record?.[`${board}_url`];
  const children = [];
  if (url) children.push(el('img', { src: url, alt: `${record.name}’s ${BOARDS[board].label}`, 'data-zoom': board }));
  else if (board === 'emotions' && record?.sheet_url && !drawing.has(record.id)) {
    children.push(el('div', { class: 'board-empty' },
      el('p', { text: `Show how ${record.name} looks happy, sad, angry, surprised, scared, and thinking.` }),
      el('button', { class: 'secondary-button', type: 'button', 'data-draw': 'emotions', text: `Draw emotion board · ${usd(library.image_cost_usd)}` })));
  } else {
    children.push(el('p', {
      class: 'board-empty',
      text: board === 'sheet' ? 'Your character sheet appears here: side, front, and back views plus face close-ups.' : 'Drawn from the character sheet, so the face stays the same in every emotion.',
    }));
  }
  if (record && drawing.get(record.id) === board) {
    children.push(el('div', { class: 'board-busy', role: 'status' },
      el('span', { text: BOARDS[board].busy }),
      el('div', { class: 'loader', 'aria-hidden': 'true' }, el('span')),
      el('small', { text: 'About a minute. You can go back to the library meanwhile.' })));
  }
  frame.replaceChildren(...children);
}

function staleNotice(record, board) {
  const notice = $(`${BOARDS[board].card} [data-stale]`);
  const stale = record?.[`${board}_stale`] && !drawing.has(record.id);
  notice.hidden = !stale;
  if (!stale) return;
  notice.replaceChildren(
    el('span', { text: board === 'sheet' ? 'Your details or reference images changed. Update the sheet to match?' : 'This board still shows the previous character sheet.' }),
    el('button', { class: 'secondary-button', type: 'button', 'data-draw': board, 'data-mode': board === 'emotions' ? 'fresh' : 'update', text: board === 'sheet' ? 'Update sheet' : 'Redraw board' }),
  );
}

function renderEditor() {
  const record = current;
  const busy = record && drawing.has(record.id);
  const cost = library.image_cost_usd;
  $('#formEyebrow').textContent = record ? 'Edit character' : 'New character';
  $('#formTitle').textContent = record?.name || 'Who are they?';
  $('#saveButton').textContent = !record ? 'Draw my character' : record.sheet ? 'Save changes' : 'Save and draw';
  $('#saveButton').disabled = Boolean(busy);
  $('#formCaption').textContent = !record?.sheet
    ? `Draws a character sheet, then an emotion board · about ${usd(cost * 2)}`
    : `Saving is free. Each redraw costs about ${usd(cost)}.`;
  $$('input, textarea', form).forEach(input => { input.disabled = Boolean(busy); });
  renderRefs(Boolean(busy));

  Object.keys(BOARDS).forEach(board => {
    const card = $(BOARDS[board].card);
    boardFrame(record, board);
    staleNotice(record, board);
    const tweak = $(`[data-tweak="${board}"]`);
    tweak.hidden = !record?.[board] || busy;
    const message = record && errors.get(`${record.id}:${board}`);
    const error = $('[data-error]', card);
    error.hidden = !message;
    error.textContent = message || '';
  });
  $('#emotionChips').replaceChildren(...library.emotions.map(emotion => el('li', { text: emotion[0].toUpperCase() + emotion.slice(1) })));
  $('#editorFoot').hidden = !record;
  $('#spent').textContent = record?.spent_usd ? `About ${usd(record.spent_usd)} spent on drawings` : '';
  $('#deleteButton').disabled = Boolean(busy);
  renderReturnNote();
}

function render() {
  if (!$('#editorView').hidden) renderEditor();
  else renderLibrary();
}

async function draw(id, board, body = {}) {
  drawing.set(id, board);
  errors.delete(`${id}:${board}`);
  render();
  try {
    upsert(await api('POST', `/api/characters/${id}/${board}`, body));
    if (current?.id === id) $(`[data-tweak="${board}"] input`).value = '';
    return true;
  } catch (error) {
    errors.set(`${id}:${board}`, error.message);
    return false;
  } finally {
    drawing.delete(id);
    render();
  }
}

async function drawEverything(id) {
  if (await draw(id, 'sheet')) await draw(id, 'emotions');
}

async function save(event) {
  event.preventDefault();
  const details = formDetails();
  const missing = ['name', 'kind'].filter(name => !details[name]);
  $$('[aria-invalid]', form).forEach(input => input.removeAttribute('aria-invalid'));
  missing.forEach(name => form.elements[name].setAttribute('aria-invalid', 'true'));
  const formError = $('#formError');
  if (missing.length) {
    formError.textContent = 'Give your character a name and say who or what they are.';
    formError.hidden = false;
    form.elements[missing[0]].focus();
    return;
  }
  formError.hidden = true;
  $('#saveButton').disabled = true;
  try {
    let record = current
      ? await api('PUT', `/api/characters/${current.id}`, details)
      : await api('POST', '/api/characters', details);
    if (!current) {
      storage('remove');
      history.replaceState(null, '', `#${record.id}`);
    }
    current = record;
    upsert(record);
    // Images picked before the character existed upload now.
    const failed = [];
    for (const image of pendingRefs) {
      try {
        record = await api('POST', `/api/characters/${record.id}/references`, { image });
        upsert(record);
      } catch (error) {
        failed.push(error.message);
      }
    }
    pendingRefs = [];
    renderEditor();
    if (failed.length) {
      // Drawing without the images the user picked would cost money for the wrong look.
      throw new Error(`${failed.length} reference image${failed.length > 1 ? 's' : ''} didn’t upload (${failed[0]}). Add ${failed.length > 1 ? 'them' : 'it'} again, then press Save and draw.`);
    }
    if (!record.sheet) await drawEverything(record.id);
    else toast(record.sheet_stale ? 'Saved. Update the sheet to see your changes.' : 'Saved.');
  } catch (error) {
    formError.textContent = error.message;
    formError.hidden = false;
  } finally {
    renderEditor();
  }
}

// ---------- Events ----------
form.addEventListener('submit', save);
form.addEventListener('input', () => {
  if (!current) storage('set', formDetails());
  $('#formError').hidden = true;
});
form.addEventListener('change', () => { if (!current) storage('set', formDetails()); });

form.addEventListener('change', event => {
  if (!event.target.matches('[data-ref-input]')) return;
  addReferences(event.target.files);
  event.target.value = '';
});
form.addEventListener('click', event => {
  const remove = event.target.closest('[data-remove-ref]');
  if (remove) removeReference(remove.dataset.removeRef);
});
const refs = $('#refs');
refs.addEventListener('dragover', event => {
  if (!event.dataTransfer?.types.includes('Files') || $('#saveButton').disabled) return;
  event.preventDefault();
  refs.classList.add('is-dragging');
});
refs.addEventListener('dragleave', () => refs.classList.remove('is-dragging'));
refs.addEventListener('drop', event => {
  event.preventDefault();
  refs.classList.remove('is-dragging');
  addReferences(event.dataTransfer.files);
});

$$('form.tweak').forEach(tweak => tweak.addEventListener('submit', event => {
  event.preventDefault();
  const note = tweak.elements.note.value.trim();
  if (!note) { tweak.elements.note.focus(); return; }
  if (current) draw(current.id, tweak.dataset.tweak, { note });
}));

document.addEventListener('click', event => {
  if (!current) return;
  const trigger = event.target.closest('[data-draw], [data-fresh]');
  if (trigger) {
    const board = trigger.dataset.draw || trigger.dataset.fresh;
    draw(current.id, board, trigger.dataset.fresh || trigger.dataset.mode === 'fresh' ? { fresh: true } : {});
    return;
  }
  const zoom = event.target.closest('[data-zoom]');
  if (zoom) {
    $('#imageTitle').textContent = `${current.name} · ${BOARDS[zoom.dataset.zoom].label}`;
    $('#imageFull').src = zoom.src;
    $('#imageFull').alt = zoom.alt;
    $('#imageDialog').showModal();
  }
});

$('#returnNote').addEventListener('click', event => {
  if (event.target.closest('[data-return]')) forgetReturn();
});

$('#imageDialog').addEventListener('click', event => {
  if (event.target === event.currentTarget || event.target.closest('[data-close]')) event.currentTarget.close();
});

$('#deleteButton').addEventListener('click', () => {
  $('#deleteName').textContent = current.name;
  $('#deleteDialog').returnValue = '';
  $('#deleteDialog').showModal();
});

$('#deleteDialog').addEventListener('close', async () => {
  if ($('#deleteDialog').returnValue !== 'delete' || !current) return;
  const { id, name } = current;
  try {
    await api('DELETE', `/api/characters/${id}`);
    library.characters = library.characters.filter(item => item.id !== id);
    location.hash = '';
    toast(`${name} was deleted.`);
  } catch (error) {
    toast(error.message);
  }
});

window.addEventListener('hashchange', route);
window.addEventListener('beforeunload', event => {
  if (drawing.size) event.preventDefault();
});

// ---------- Boot ----------
(async function boot() {
  const { clerk } = await loadClerk().catch(() => ({ clerk: null }));
  if (clerk && !clerk.isSignedIn) {
    goToSignIn();
    return;
  }
  if (clerk) clerk.mountUserButton($('#userButton'));
  if (!(await ensureAccess(clerk))) return;
  try {
    library = await api('GET', '/api/characters');
  } catch (error) {
    $('#libraryError').textContent = error.message;
    $('#libraryError').hidden = false;
  }
  route();
})();
