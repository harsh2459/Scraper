import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

// ── Global fetch guard ─────────────────────────────────────────────────────
// Send the session cookie on every request, and if any API call comes back
// 401 (session expired / not signed in) bounce to the login page. One place,
// so individual pages don't each need to handle auth.
//
// Scoped to TM Manager's OWN backend only — the separate scraper service
// (paths under /scraper-api, see lib/api.ts's SCRAPER_API_BASE) has its own
// unrelated ops-only Basic-Auth gate, and a 401 from THAT service means
// nothing about the user's TM Manager session. Without this scope, a
// scraper-side auth hiccup (e.g. during a Word Mark search) incorrectly
// forced a full logout/redirect instead of surfacing as an in-page error.
const _origFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init: RequestInit = {}) => {
  const res = await _origFetch(input, { credentials: 'same-origin', ...init })
  if (res.status === 401) {
    const url = typeof input === 'string' ? input : input.toString()
    const isScraperRequest = url.includes('/scraper-api');
    // don't loop on the login/auth endpoints themselves, and don't treat
    // the separate scraper service's own auth as this app's session
    if (!isScraperRequest && !/\/(login|api\/login|api\/logout|api\/me)\b/.test(url)) {
      const next = encodeURIComponent(window.location.pathname + window.location.search)
      window.location.href = `/login?next=${next}`
    }
  }
  return res
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
