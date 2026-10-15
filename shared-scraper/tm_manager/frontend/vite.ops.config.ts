import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import path from 'node:path';

export default defineConfig(({ mode }) => {
  const worker = mode === 'worker';
  const directory = import.meta.dirname;
  const modules = path.resolve(directory, 'node_modules');
  return {
    root: path.resolve(directory, '../../ops_frontend'),
    base: worker ? '/' : '/admin/',
    plugins: [react(), tailwindcss()],
    resolve: { alias: Object.fromEntries(['react', 'react-dom', 'react-router-dom', 'lucide-react'].map(name => [name, path.join(modules, name)])) },
    build: { outDir: path.resolve(directory, worker ? '../../worker_frontend/dist' : '../../admin_frontend/dist'), emptyOutDir: true },
    server: { host: '127.0.0.1', port: worker ? 5181 : 5180, fs: { allow: [path.resolve(directory, '../..')] }, proxy: {
      '/api/control/v1/admin': 'http://127.0.0.1:5005', '/api/control': 'http://127.0.0.1:5010', '/api/worker': 'http://127.0.0.1:5011', '/api': 'http://127.0.0.1:5005',
    } },
  };
});
