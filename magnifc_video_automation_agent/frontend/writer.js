// Story writer: a chat that drafts and revises one story; "Use this story" hands it to the Create prompt.
import { loadLibrary, mentionedCharacters, setupMentions } from './mentions.js';

const KEY = 'studio.writer.v1';
// Read by the Characters page: a new character's prefilled details, and where to send the user back to.
const CHARACTER_DRAFT_KEY = 'characters.draft.v1';
const RETURN_KEY = 'characters.returnTo';
const MAX_HISTORY = 20; // messages sent to the server; older drafts stay on screen only
const IDEAS = [
  'A bedtime story about a little cloud that is afraid of thunder',
  'Two street dogs in Mumbai who find a lost kitten',
  'A grandmother teaches her granddaughter to fly a kite on Makar Sankranti',
];

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

function load() {
  try { return JSON.parse(localStorage.getItem(KEY) || '[]'); } catch { return []; }
}

function save(messages) {
  try { localStorage.setItem(KEY, JSON.stringify(messages)); } catch { /* storage unavailable */ }
}

/**
 * messages: { role: 'user', text } | { role: 'error', text }
 *   | { role: 'writer', title, story, reply, characters: [{ name, kind, appearance }], dismissed: [name] }
 * post(url, body) and settings() come from the studio; onUse(story) fills the Create prompt.
 */
export function setupWriter({ post, settings, onUse, toast }) {
  const log = document.querySelector('#chatLog');
  const chat = document.querySelector('#chatForm');
  const input = document.querySelector('#chatInput');
  const send = document.querySelector('#chatSend');
  let messages = load();
  let busy = false;
  let library = [];

  function latestDraftIndex() {
    for (let index = messages.length - 1; index >= 0; index -= 1) if (messages[index].role === 'writer') return index;
    return -1;
  }

  function storyCard(message, index, latest) {
    return el('article', { class: `draft${latest ? ' is-latest' : ''}` },
      el('h3', { class: 'draft-title', text: message.title }),
      ...message.story.split(/\n{2,}/).map(paragraph => el('p', { text: paragraph.trim() })),
      el('div', { class: 'draft-actions' },
        el('button', { class: latest ? 'primary-button compact' : 'secondary-button', type: 'button', 'data-use': index, text: latest ? 'Use this story' : 'Use this version' }),
        el('button', { class: 'link-button', type: 'button', 'data-copy': index, text: 'Copy' }),
        el('span', { class: 'draft-count', text: `${message.story.trim().split(/\s+/).length} words` })),
      latest ? castCheck(message, index) : null);
  }

  // Who is in the latest draft: saved characters, and the rest with ways to replace or save them.
  function castCheck(message, index) {
    const saved = mentionedCharacters(message.story);
    const open = (message.characters || []).filter(character => !(message.dismissed || []).includes(character.name));
    if (!saved.length && !open.length) return null;
    const drawn = library.filter(character => character.sheet_url);
    const savedRows = saved.map(character => el('li', { class: 'cast-item is-saved' },
      el('span', { class: 'cast-who' }, el('strong', { text: character.name }), el('small', { text: 'In your character library' }))));
    const openRows = open.map(character => {
      const match = drawn.find(item => item.name.toLocaleLowerCase() === character.name.toLocaleLowerCase());
      const label = `Swap ${character.name} for a saved character`;
      return el('li', { class: 'cast-item' },
        el('span', { class: 'cast-who' }, el('strong', { text: character.name }), el('small', { text: character.kind })),
        el('span', { class: 'cast-actions' },
          match
            ? el('button', { class: 'secondary-button', type: 'button', 'data-swap': character.name, 'data-to': match.id, text: `Use saved @${match.name}` })
            : drawn.length
              ? el('span', { class: 'select plain' }, el('select', { 'data-swap-select': character.name, 'aria-label': label },
                el('option', { value: '', text: 'Swap for a saved character…' }),
                ...drawn.map(item => el('option', { value: item.id, text: `@${item.name} · ${item.kind}` }))))
              : null,
          match ? null : el('button', { class: 'secondary-button', type: 'button', 'data-create': character.name, 'data-message': index, text: 'Create character' }),
          el('button', { class: 'link-button', type: 'button', 'data-dismiss': character.name, 'data-message': index, text: 'Let AI design them' })));
    });
    return el('section', { class: 'cast-check', 'aria-label': 'Cast check' },
      savedRows.length ? el('h4', { text: 'In your library' }) : null,
      savedRows.length ? el('ul', {}, ...savedRows) : null,
      openRows.length ? el('h4', { text: 'Not in your library yet' }) : null,
      openRows.length ? el('p', { text: 'Swap them for a saved character so they look the same in every film, save them as a new character, or let the film design them.' }) : null,
      openRows.length ? el('ul', {}, ...openRows) : null);
  }

  function swap(name, characterId) {
    const character = library.find(item => item.id === characterId);
    if (!character) return;
    submit(character.name.toLocaleLowerCase() === name.toLocaleLowerCase()
      ? `Write ${name} as @${character.name}, my saved character, everywhere in the story. Keep everything else the same.`
      : `Replace ${name} with @${character.name} (${character.kind}) everywhere in the story. Keep everything else the same.`);
  }

  function createCharacter(message, name) {
    const character = message.characters.find(item => item.name === name);
    try {
      localStorage.setItem(CHARACTER_DRAFT_KEY, JSON.stringify({
        name: character.name, kind: character.kind, appearance: character.appearance, outfit: '', props: '', visual_style: settings().style,
      }));
      localStorage.setItem(RETURN_KEY, JSON.stringify({ href: '/#writer', name: character.name }));
    } catch { /* storage unavailable: the form simply opens empty */ }
    location.href = '/characters#new';
  }

  function render() {
    const latest = latestDraftIndex();
    const nodes = messages.map((message, index) => {
      if (message.role === 'user') return el('div', { class: 'bubble from-user' }, el('p', { text: message.text }));
      if (message.role === 'error') {
        return el('div', { class: 'bubble from-writer is-error' }, el('p', { text: message.text }),
          index === messages.length - 1 ? el('button', { class: 'link-button', type: 'button', 'data-retry': '', text: 'Try again' }) : null);
      }
      return el('div', { class: 'bubble from-writer' }, el('p', { class: 'reply', text: message.reply }), storyCard(message, index, index === latest));
    });
    if (busy) {
      nodes.push(el('div', { class: 'bubble from-writer', role: 'status' },
        el('span', { class: 'typing', 'aria-label': 'Writing your story' }, el('span'), el('span'), el('span'))));
    }
    if (!messages.length) {
      nodes.push(el('div', { class: 'chat-empty' },
        el('p', { text: 'Tell me an idea, as short or long as you like. I’ll write a story sized for your film, then you can give notes until it feels right.' }),
        el('div', { class: 'ideas' }, ...IDEAS.map(idea => el('button', { class: 'chip', type: 'button', 'data-idea': idea, text: idea })))));
    }
    log.replaceChildren(...nodes);
    // A new draft is read from its title; while waiting, keep the typing dots in view.
    if (messages.length) nodes.at(-1).scrollIntoView({ block: busy ? 'end' : 'start' });
    send.disabled = busy;
    input.disabled = busy;
    document.querySelector('#writerReset').hidden = !messages.length;
    const { duration, language } = settings();
    document.querySelector('#writerSettings').textContent = `Stories are sized for a ${duration}-second film in ${language}. Change both on Create.`;
  }

  // The server sees drafts as the writer's turns, so a note always revises the latest one.
  function history() {
    return messages
      .filter(message => message.role !== 'error')
      .map(message => (message.role === 'user'
        ? { role: 'user', content: message.text }
        : { role: 'assistant', content: `${message.title}\n\n${message.story}` }))
      .slice(-MAX_HISTORY);
  }

  async function ask() {
    busy = true;
    render();
    const { duration, language } = settings();
    try {
      const data = await post('/api/story-writer', { messages: history(), duration, language });
      messages.push({ role: 'writer', title: data.title, story: data.story, reply: data.reply, characters: data.characters || [], dismissed: [] });
    } catch (error) {
      messages.push({ role: 'error', text: error.message });
    } finally {
      busy = false;
      save(messages);
      render();
      input.focus();
    }
  }

  function submit(text) {
    const value = text.trim();
    if (!value || busy) return;
    if (messages.at(-1)?.role === 'error') messages.pop();
    messages.push({ role: 'user', text: value });
    input.value = '';
    save(messages);
    ask();
  }

  chat.addEventListener('submit', event => { event.preventDefault(); submit(input.value); });
  input.addEventListener('keydown', event => {
    // Enter sends unless the @ picker is open (it uses Enter to pick a character).
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && document.querySelector('#chatMentionMenu').hidden) {
      event.preventDefault();
      submit(input.value);
    }
  });
  log.addEventListener('click', async event => {
    const idea = event.target.closest('[data-idea]');
    if (idea) return submit(idea.dataset.idea);
    if (event.target.closest('[data-retry]')) {
      messages.pop();
      return ask();
    }
    const use = event.target.closest('[data-use]');
    if (use) return onUse(messages[Number(use.dataset.use)].story);
    const swapButton = event.target.closest('[data-swap]');
    if (swapButton) return swap(swapButton.dataset.swap, swapButton.dataset.to);
    const create = event.target.closest('[data-create]');
    if (create) return createCharacter(messages[Number(create.dataset.message)], create.dataset.create);
    const dismiss = event.target.closest('[data-dismiss]');
    if (dismiss) {
      const message = messages[Number(dismiss.dataset.message)];
      message.dismissed = [...(message.dismissed || []), dismiss.dataset.dismiss];
      save(messages);
      return render();
    }
    const copy = event.target.closest('[data-copy]');
    if (copy) {
      try {
        await navigator.clipboard.writeText(messages[Number(copy.dataset.copy)].story);
        toast('Story copied.');
      } catch {
        toast('Could not copy. Select the text instead.');
      }
    }
    return undefined;
  });
  log.addEventListener('change', event => {
    const select = event.target.closest('[data-swap-select]');
    if (select?.value) swap(select.dataset.swapSelect, select.value);
  });
  document.querySelector('#writerReset').addEventListener('click', () => {
    if (busy) return;
    messages = [];
    save(messages);
    render();
    input.focus();
  });

  setupMentions({ form: chat, textarea: input, menu: document.querySelector('#chatMentionMenu') });
  render();
  loadLibrary().then(list => { library = list; render(); });
  return { render };
}
