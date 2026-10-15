// Relative URLs so the same build works in dev (Vite proxy), on the LAN
// (served from :5005), and in production (nginx serves the static build and
// reverse-proxies /api, /trademark/, etc. to the tm_manager backend).
export const API_BASE = '';

// The scraper app is a separate process. In production it is NOT exposed
// publicly — nginx reverse-proxies /scraper-api/* to it on 127.0.0.1:5001.
// In dev, vite.config.ts proxies /scraper-api the same way.
// VITE_SCRAPER_API_BASE can override (e.g. a direct http://host:5001 on a
// trusted LAN) — default is the same-origin /scraper-api path.
export const SCRAPER_API_BASE =
  (import.meta as any).env?.VITE_SCRAPER_API_BASE || '/scraper-api';
