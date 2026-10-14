import { loadClerk, safeRedirect } from './auth.js';

const MODES = {
  '/sign-up': {
    title: 'Create your account.',
    lede: 'Make your first film in about 10 minutes. Write one line, approve the storyboard, download the film.',
    switchText: 'Already have an account?',
    switchLink: { href: '/sign-in', label: 'Sign in' },
    pageTitle: 'Create account · Studio',
    mount: (clerk, element, redirect) => clerk.mountSignUp(element, { routing: 'hash', signInUrl: '/sign-in', fallbackRedirectUrl: redirect }),
  },
  '/sign-in': {
    title: 'Welcome back.',
    lede: 'Sign in to pick up your stories and films.',
    switchText: 'New to Studio?',
    switchLink: { href: '/sign-up', label: 'Create an account' },
    pageTitle: 'Sign in · Studio',
    mount: (clerk, element, redirect) => clerk.mountSignIn(element, { routing: 'hash', signUpUrl: '/sign-up', fallbackRedirectUrl: redirect }),
  },
};

const mode = MODES[location.pathname] || MODES['/sign-in'];
const redirect = safeRedirect(new URLSearchParams(location.search).get('redirect_url'));
const mountPoint = document.getElementById('authMount');

function renderChrome() {
  document.title = mode.pageTitle;
  document.getElementById('authTitle').textContent = mode.title;
  document.getElementById('authLede').textContent = mode.lede;
  const switcher = document.getElementById('authSwitch');
  const link = document.createElement('a');
  // Carry the destination across, so switching to sign-up still returns people where they were going.
  link.href = redirect === '/' ? mode.switchLink.href : `${mode.switchLink.href}?redirect_url=${encodeURIComponent(redirect)}`;
  link.textContent = mode.switchLink.label;
  switcher.replaceChildren(`${mode.switchText} `, link);
}

function renderNotice(message, detail) {
  const notice = document.createElement('div');
  notice.className = 'auth-notice';
  const heading = document.createElement('p');
  heading.className = 'auth-notice-title';
  heading.textContent = message;
  const body = document.createElement('p');
  body.textContent = detail;
  const open = document.createElement('a');
  open.className = 'primary-button compact';
  open.href = '/';
  open.textContent = 'Open the studio';
  notice.append(heading, body, open);
  mountPoint.replaceChildren(notice);
}

function playFilm() {
  const film = document.getElementById('authFilm');
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  film.play().catch(() => { /* autoplay blocked: the poster stays */ });
}

(async function boot() {
  renderChrome();
  playFilm();
  try {
    const { clerk, config } = await loadClerk();
    if (!clerk) {
      renderNotice(
        "Sign-in isn't set up on this server.",
        config.error
          ? `${config.error} Fix it in .env and restart the server.`
          : 'Add CLERK_PUBLISHABLE_KEY to .env and restart the server. Until then the studio is open to anyone who can reach it.',
      );
      return;
    }
    if (clerk.isSignedIn) {
      location.replace(redirect);
      return;
    }
    mountPoint.replaceChildren();
    mode.mount(clerk, mountPoint, redirect);
  } catch (error) {
    renderNotice('Sign-in could not load.', `${error.message}. Check your connection and reload the page.`);
  }
})();
