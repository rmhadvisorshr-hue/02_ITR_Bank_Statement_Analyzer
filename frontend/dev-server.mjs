import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.join(__dirname, 'public');
const PORT = parseInt(process.env.PORT || '5500', 10);
const BACKEND_URL = (process.env.BACKEND_URL || 'http://localhost:8765').replace(/\/$/, '');

const MIME = {
  '.html': 'text/html',
  '.css': 'text/css',
  '.js': 'application/javascript',
  '.json': 'application/json',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
};

// Same {{BASE_PATH}} placeholder backend/server.mjs fills in when it serves these
// pages directly - here there's no reverse proxy, so it's always empty. Also fills
// in {{BACKEND_URL}} so the frontend knows where to send API requests, since this
// server only ever serves static files for THIS project - it never talks to Python
// or spawns the backend.
async function serveTemplate(res, file) {
  const html = await fs.readFile(path.join(PUBLIC_DIR, file), 'utf-8');
  const body = html.split('{{BASE_PATH}}').join('').split('{{BACKEND_URL}}').join(BACKEND_URL);
  res.writeHead(200, { 'Content-Type': 'text/html' });
  res.end(body);
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  try {
    if (url.pathname === '/') return await serveTemplate(res, 'index.html');
    if (url.pathname === '/reports') return await serveTemplate(res, 'reports.html');

    const resolved = path.resolve(path.join(PUBLIC_DIR, decodeURIComponent(url.pathname)));
    if (!resolved.startsWith(PUBLIC_DIR)) {
      res.writeHead(403).end();
      return;
    }
    const data = await fs.readFile(resolved);
    res.writeHead(200, { 'Content-Type': MIME[path.extname(resolved)] || 'application/octet-stream' });
    res.end(data);
  } catch {
    res.writeHead(404, { 'Content-Type': 'text/plain' });
    res.end('Not found');
  }
});

server.listen(PORT, () => {
  console.log('ITR Bank Statement Analyzer frontend (static, no backend spawned)');
  console.log(`URL: http://localhost:${PORT}/`);
  console.log(`Backend: ${BACKEND_URL}`);
});
