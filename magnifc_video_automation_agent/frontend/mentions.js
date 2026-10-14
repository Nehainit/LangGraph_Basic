// "@" in the story box picks characters from the user's library; mentioned characters are sent with the film request.
import { authHeaders } from './auth.js';

const STYLE_NAMES = { realistic: 'Realistic', cinematic: 'Cinematic', illustrated: 'Illustrated', '3d_animation': '3D animation', anime: 'Anime' };
const MAX_OPTIONS = 8;
const MIRROR_PROPS = [
  'boxSizing', 'width', 'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft', 'borderTopWidth', 'borderRightWidth',
  'borderBottomWidth', 'borderLeftWidth', 'fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'letterSpacing', 'wordSpacing', 'textTransform',
];

let library = null;
let loading = null;

export function loadLibrary() {
  if (library) return Promise.resolve(library);
  loading ??= (async () => {
    try {
      const response = await fetch('/api/characters', { headers: await authHeaders() });
      library = response.ok ? (await response.json()).characters || [] : [];
    } catch {
      library = [];
    }
    return library;
  })();
  return loading;
}

const escapeRegExp = text => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** Library characters with a drawn sheet whose @Name appears in the text. */
export function mentionedCharacters(text) {
  return (library || []).filter(character => character.sheet_url
    && new RegExp(`@${escapeRegExp(character.name)}(?![\\p{L}\\p{N}_])`, 'u').test(text));
}

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

function avatar(character) {
  return el('span', { class: 'mention-avatar', 'aria-hidden': 'true' },
    character.sheet_url ? el('img', { src: character.sheet_url, alt: '' }) : character.name.trim().slice(0, 1).toUpperCase());
}

// Where the caret sits inside a textarea, measured with an invisible copy of its text.
function caretPoint(textarea, index) {
  const style = getComputedStyle(textarea);
  const mirror = document.createElement('div');
  MIRROR_PROPS.forEach(prop => { mirror.style[prop] = style[prop]; });
  Object.assign(mirror.style, { position: 'absolute', visibility: 'hidden', top: '0', left: '0', whiteSpace: 'pre-wrap', overflowWrap: 'break-word' });
  mirror.textContent = textarea.value.slice(0, index);
  const marker = el('span', { text: '​' });
  mirror.append(marker);
  document.body.append(mirror);
  const point = { top: marker.offsetTop + marker.offsetHeight - textarea.scrollTop, left: marker.offsetLeft };
  mirror.remove();
  return point;
}

export function setupMentions({ form, textarea, menu, cast, styleOf, useStyle }) {
  let query = null; // { start, text } of the @word being typed
  let items = [];
  let active = -1;

  function queryAtCaret() {
    if (textarea.selectionStart !== textarea.selectionEnd) return null;
    const before = textarea.value.slice(0, textarea.selectionStart);
    const match = before.match(/(?:^|\s)@([^\s@]{0,40})$/u);
    return match ? { start: before.length - match[1].length - 1, text: match[1] } : null;
  }

  function close() {
    query = null;
    menu.hidden = true;
    textarea.setAttribute('aria-expanded', 'false');
    textarea.removeAttribute('aria-activedescendant');
  }

  function step(direction) {
    const enabled = items.map((item, index) => (item.sheet_url ? index : -1)).filter(index => index >= 0);
    if (!enabled.length) return;
    const position = enabled.indexOf(active);
    active = enabled[(position + direction + enabled.length) % enabled.length];
    render();
  }

  function render() {
    const options = items.map((character, index) => el('div', {
      class: `mention-option${index === active ? ' is-active' : ''}`,
      id: `mention-${index}`,
      role: 'option',
      'aria-selected': String(index === active),
      'aria-disabled': character.sheet_url ? null : 'true',
      'data-index': index,
    },
      avatar(character),
      el('span', { class: 'mention-text' }, el('strong', { text: character.name }), el('small', { text: character.kind })),
      el('span', { class: 'mention-style', text: character.sheet_url ? STYLE_NAMES[character.visual_style] || '' : 'Not drawn yet' })));
    const empty = el('div', { class: 'mention-empty' },
      document.createTextNode(library.length ? `No saved character matches “${query.text}”. ` : 'Your character library is empty. '),
      el('a', { href: '/characters#new', text: 'Create a character' }));
    menu.replaceChildren(...(options.length ? options : [empty]));
    if (active >= 0) textarea.setAttribute('aria-activedescendant', `mention-${active}`);
    else textarea.removeAttribute('aria-activedescendant');
  }

  function place() {
    menu.hidden = false;
    const point = caretPoint(textarea, query.start);
    const box = menu.offsetParent || textarea.parentElement;
    menu.style.top = `${textarea.offsetTop + point.top + 6}px`;
    menu.style.left = `${Math.max(8, Math.min(textarea.offsetLeft + point.left, box.clientWidth - menu.offsetWidth - 8))}px`;
    textarea.setAttribute('aria-expanded', 'true');
  }

  async function update() {
    if (!queryAtCaret()) return close();
    await loadLibrary();
    query = queryAtCaret();
    if (!query) return close();
    const text = query.text.toLocaleLowerCase();
    items = library
      .filter(character => character.name.toLocaleLowerCase().includes(text))
      .sort((a, b) => Number(!a.name.toLocaleLowerCase().startsWith(text)) - Number(!b.name.toLocaleLowerCase().startsWith(text)))
      .slice(0, MAX_OPTIONS);
    active = items.findIndex(character => character.sheet_url);
    render();
    place();
  }

  function choose(index) {
    const character = items[index];
    if (!character?.sheet_url || !query) return;
    const caret = textarea.selectionStart;
    const insert = `@${character.name} `;
    textarea.setRangeText(insert, query.start, caret, 'end');
    close();
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    textarea.focus();
  }

  function renderCast() {
    if (!cast) return;
    const characters = mentionedCharacters(textarea.value);
    cast.hidden = !characters.length;
    if (!characters.length) return;
    const style = styleOf();
    const styles = [...new Set(characters.map(character => character.visual_style))];
    let hint = null;
    if (styles.length > 1) {
      hint = el('p', { class: 'cast-hint', text: 'Your cast was drawn in different styles. The film uses one style, so some may look different from their sheets.' });
    } else if (styles[0] !== style) {
      const who = characters.length === 1 ? `${characters[0].name} is` : 'Your cast is';
      hint = el('p', { class: 'cast-hint' },
        document.createTextNode(`${who} drawn in ${STYLE_NAMES[styles[0]]}. `),
        el('button', { class: 'link-button', type: 'button', 'data-cast-style': styles[0], text: `Use ${STYLE_NAMES[styles[0]]} for this film` }));
    }
    cast.replaceChildren(
      el('span', { class: 'cast-label', text: 'Cast' }),
      ...characters.map(character => el('a', { class: 'cast-chip', href: `/characters#${character.id}`, title: `Open ${character.name} in your library` },
        avatar(character), el('span', { text: character.name }))),
      ...(hint ? [hint] : []),
    );
  }

  textarea.setAttribute('aria-autocomplete', 'list');
  textarea.setAttribute('aria-controls', menu.id);
  textarea.setAttribute('aria-expanded', 'false');
  textarea.addEventListener('input', () => { update(); renderCast(); });
  textarea.addEventListener('click', update);
  textarea.addEventListener('blur', close);
  textarea.addEventListener('scroll', close);
  textarea.addEventListener('keydown', event => {
    if (menu.hidden) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      step(event.key === 'ArrowDown' ? 1 : -1);
    } else if ((event.key === 'Enter' || event.key === 'Tab') && !event.metaKey && !event.ctrlKey && active >= 0) {
      event.preventDefault();
      choose(active);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      close();
    }
  });
  // mousedown keeps focus in the textarea, so the caret position survives the click.
  menu.addEventListener('mousedown', event => {
    event.preventDefault();
    const option = event.target.closest('[data-index]');
    if (option) choose(Number(option.dataset.index));
  });
  cast?.addEventListener('click', event => {
    const button = event.target.closest('[data-cast-style]');
    if (button) useStyle(button.dataset.castStyle);
  });
  form.addEventListener('change', renderCast);
  loadLibrary().then(renderCast);
  return { renderCast };
}
