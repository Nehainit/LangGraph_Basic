// Loads Clerk when the server has sign-in configured. Without CLERK_PUBLISHABLE_KEY the studio runs open for local development.

const APPEARANCE = {
  // The studio's dark theme, so sign-in and the account menu match the rest of the site.
  variables: {
    colorPrimary: '#2f74f5',
    colorPrimaryForeground: '#ffffff',
    colorBackground: '#151a22',
    colorForeground: '#e9edf4',
    colorMutedForeground: '#a3adbd',
    colorInput: '#0d1016',
    colorInputForeground: '#e9edf4',
    colorNeutral: '#e9edf4',
    colorBorder: '#3a4357',
    colorMuted: '#1c2330',
    colorDanger: '#ff8f8f',
    colorShadow: '#000000',
    colorModalBackdrop: 'rgba(0, 0, 0, 0.6)',
    fontFamily: "'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    borderRadius: '12px',
  },
};

let clerkPromise;

function loadScript(src, attributes = {}) {
  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = src;
    script.crossOrigin = 'anonymous';
    script.async = true;
    for (const [name, value] of Object.entries(attributes)) script.setAttribute(name, value);
    script.onload = resolve;
    script.onerror = () => reject(new Error(`Could not load ${src}`));
    document.head.append(script);
  });
}

async function fetchConfig() {
  try {
    const response = await fetch('/api/auth-config');
    return response.ok ? await response.json() : { enabled: false };
  } catch {
    return { enabled: false };
  }
}

/** Resolves to { clerk, config }; clerk is null when sign-in is not configured on the server. */
export function loadClerk() {
  clerkPromise ??= (async () => {
    const config = await fetchConfig();
    if (!config.enabled) return { clerk: null, config };
    const base = `https://${config.frontend_api}/npm`;
    await Promise.all([
      loadScript(`${base}/@clerk/ui@1/dist/ui.browser.js`),
      loadScript(`${base}/@clerk/clerk-js@6/dist/clerk.browser.js`, { 'data-clerk-publishable-key': config.publishable_key }),
    ]);
    const clerk = typeof window.Clerk === 'function' ? new window.Clerk(config.publishable_key) : window.Clerk;
    await clerk.load({
      ui: { ClerkUI: window.__internal_ClerkUICtor },
      signInUrl: '/sign-in',
      signUpUrl: '/sign-up',
      afterSignOutUrl: '/sign-in',
      appearance: APPEARANCE,
    });
    return { clerk, config };
  })();
  return clerkPromise;
}

/** Authorization header for API calls; empty when signed out or sign-in is off. */
export async function authHeaders() {
  try {
    const { clerk } = await loadClerk();
    const token = clerk?.session ? await clerk.session.getToken() : null;
    return token ? { Authorization: `Bearer ${token}` } : {};
  } catch {
    // The server answers 401 without a token, and the studio then sends people to sign in.
    return {};
  }
}

/** Only same-site paths are followed after sign-in, so a crafted link cannot bounce people elsewhere. */
export function safeRedirect(value, fallback = '/') {
  if (typeof value !== 'string' || !value.startsWith('/')) return fallback;
  try {
    // URL parsing also catches "//host" and "/\host", which browsers treat as another site.
    const url = new URL(value, location.origin);
    return url.origin === location.origin ? `${url.pathname}${url.search}${url.hash}` : fallback;
  } catch {
    return fallback;
  }
}

export function goToSignIn() {
  const here = `${location.pathname}${location.search}`;
  location.replace(`/sign-in?redirect_url=${encodeURIComponent(here)}`);
}
