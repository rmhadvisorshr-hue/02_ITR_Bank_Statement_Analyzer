// Production build for Vercel. Local dev doesn't use this - dev-server.mjs
// substitutes {{BASE_PATH}}/{{BACKEND_URL}} at request time instead. This
// script does the same substitution once, at build time, since Vercel serves
// static files with no server-side templating.
import fs from 'node:fs';
import path from 'node:path';

const ROOT = import.meta.dirname;
const PUBLIC_DIR = path.join(ROOT, 'public');
const OUT_DIR = path.join(ROOT, 'dist');
const BACKEND_URL = (process.env.BACKEND_URL || '').replace(/\/$/, '');

if (!BACKEND_URL) {
  console.warn('WARNING: BACKEND_URL env var is not set. Set it in Vercel project settings to your backend URL (e.g. https://api.yourdomain.com).');
}

fs.rmSync(OUT_DIR, { recursive: true, force: true });
fs.mkdirSync(OUT_DIR, { recursive: true });

for (const file of fs.readdirSync(PUBLIC_DIR)) {
  const src = path.join(PUBLIC_DIR, file);
  const dest = path.join(OUT_DIR, file);
  if (file.endsWith('.html')) {
    const html = fs.readFileSync(src, 'utf-8')
      .split('{{BASE_PATH}}').join('')
      .split('{{BACKEND_URL}}').join(BACKEND_URL);
    fs.writeFileSync(dest, html);
  } else {
    fs.copyFileSync(src, dest);
  }
}

console.log(`Built ${OUT_DIR} (BACKEND_URL=${BACKEND_URL || '(empty)'})`);
