// Rasterizes the TM Tracker ring mark into PNG favicons + apple-touch icon.
// Run once (or whenever the mark changes): `node scripts/gen-icons.mjs`
// Requires the `sharp` devDependency.
import sharp from 'sharp';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const OUT = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'public');

// Full-detail mark — navy gradient tile, countdown track, amber arc + blip, check.
const full = `<svg width="512" height="512" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg">
  <defs><linearGradient id="t" x1="0" y1="0" x2="512" y2="512" gradientUnits="userSpaceOnUse">
    <stop offset="0" stop-color="#1B2C57"/><stop offset="1" stop-color="#111C3B"/></linearGradient></defs>
  <rect width="512" height="512" rx="116" fill="url(#t)"/>
  <circle cx="256" cy="256" r="150" fill="none" stroke="#FFFFFF" stroke-opacity="0.13" stroke-width="30"/>
  <path d="M 256 106 A 150 150 0 1 1 150 150" fill="none" stroke="#F6A21E" stroke-width="30" stroke-linecap="round"/>
  <circle cx="150" cy="150" r="30" fill="#F6A21E"/><circle cx="150" cy="150" r="11" fill="#152449"/>
  <path d="M198 260 L240 302 L322 206" fill="none" stroke="#FFFFFF" stroke-width="34" stroke-linecap="round" stroke-linejoin="round"/>
</svg>`;

// Simplified mark for tiny sizes — flat navy, no inner track, no blip dot, fatter strokes.
const tiny = `<svg width="512" height="512" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg">
  <rect width="512" height="512" rx="116" fill="#152449"/>
  <path d="M 256 116 A 140 140 0 1 1 158 158" fill="none" stroke="#F6A21E" stroke-width="42" stroke-linecap="round"/>
  <path d="M190 262 L238 310 L326 200" fill="none" stroke="#FFFFFF" stroke-width="44" stroke-linecap="round" stroke-linejoin="round"/>
</svg>`;

const jobs = [
  { svg: full, size: 180, name: 'apple-touch-icon.png' },
  { svg: full, size: 32, name: 'favicon-32.png' },
  { svg: tiny, size: 16, name: 'favicon-16.png' },
];

for (const { svg, size, name } of jobs) {
  await sharp(Buffer.from(svg)).resize(size, size).png().toFile(resolve(OUT, name));
  console.log('wrote', name, `(${size}x${size})`);
}
