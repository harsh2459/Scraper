import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      // Keep the browser's Host header (like nginx does with `Host $http_host`): the admin proxy
      // compares it with the request Origin as a CSRF guard, so rewriting it breaks every save.
      '/api/control/v1/admin': { target: 'http://localhost:5005', changeOrigin: false },
      '/api/control': 'http://localhost:5010',
      '/admin/': 'http://localhost:5010',
      // tm_manager backend (:5005). NOTE: '/login' itself is a client-side
      // React Router page (see App.tsx), not a backend route — the real API
      // call is POST /api/login (already covered by the '/api' rule above).
      // A '/login': proxy rule here would send a plain page load of /login
      // straight to Flask, which has no GET route there and 404s instead of
      // ever letting React Router render the Login page.
      '/api': { target: 'http://localhost:5005', changeOrigin: false },
      // Only proxy the actual Flask sub-paths under /trademark/<id>/... —
      // NOT the bare `/trademark/:id` URL, which is a client-side React
      // Router route. A plain `/trademark/` prefix would send a hard
      // refresh of the detail page to Flask, which 404s it.
      '^/trademark/\\d+/(logo|document|notice|retry|remove).*': 'http://localhost:5005',
      '/journal-entry/': 'http://localhost:5005',
      '/retry-all-errors': 'http://localhost:5005',
      // scraper backend (:5001) — mirrors the production nginx setup where
      // /scraper-api/* is stripped and forwarded to the scraper.
      '/scraper-api': {
        target: 'http://localhost:5001',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/scraper-api/, ''),
      },
    },
  },
})
