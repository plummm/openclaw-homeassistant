#!/usr/bin/env node
// CDP navigation helper: attach to an existing targetId and navigate to URL,
// then wait until location.href includes a substring.

import fs from 'node:fs';
import { setTimeout as delay } from 'node:timers/promises';

const PORT = Number(process.env.CDP_PORT || '9222');
const TARGET_ID = process.env.CDP_TARGET_ID || '';
const URL = process.env.CDP_NAV_URL || '';
const WAIT_INCLUDES = process.env.CDP_WAIT_INCLUDES || '';
const WAIT_PATH_PREFIX = process.env.CDP_WAIT_PATH_PREFIX || '';
const TIMEOUT_MS = Number(process.env.CDP_TIMEOUT_MS || '30000');

if (!TARGET_ID) {
  console.error('CDP_NAV_FAIL missing CDP_TARGET_ID');
  process.exit(2);
}
if (!URL) {
  console.error('CDP_NAV_FAIL missing CDP_NAV_URL');
  process.exit(2);
}

async function fetchJson(u) {
  const res = await fetch(u);
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${u}`);
  return res.json();
}

async function getBrowserWsUrl() {
  const v = await fetchJson(`http://127.0.0.1:${PORT}/json/version`);
  if (!v?.webSocketDebuggerUrl) throw new Error('No webSocketDebuggerUrl in /json/version');
  return v.webSocketDebuggerUrl;
}

class CDP {
  constructor(wsUrl) {
    this.ws = new WebSocket(wsUrl);
    this.id = 0;
    this.pending = new Map();
  }
  async open() {
    await new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error('WS open timeout')), 5000);
      this.ws.onopen = () => { clearTimeout(t); resolve(); };
      this.ws.onerror = (e) => { clearTimeout(t); reject(new Error(`WS error: ${e?.message || e}`)); };
    });
    this.ws.onmessage = (msg) => {
      const data = JSON.parse(msg.data);
      if (data.id && this.pending.has(data.id)) {
        const { resolve, reject } = this.pending.get(data.id);
        this.pending.delete(data.id);
        if (data.error) {
          const msg = data.error.message || JSON.stringify(data.error);
          if (String(msg).includes('Inspected target navigated or closed')) resolve(null);
          else reject(new Error(msg));
        } else resolve(data.result);
      }
    };
  }
  call(method, params = {}, sessionId = undefined) {
    const id = ++this.id;
    const payload = sessionId ? { id, method, params, sessionId } : { id, method, params };
    this.ws.send(JSON.stringify(payload));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }
  close() { try { this.ws.close(); } catch {} }
}

async function waitForOk(call) {
  const start = Date.now();
  let lastHref = '';
  let lastPath = '';
  while (Date.now() - start < TIMEOUT_MS) {
    lastHref = await call('Runtime.evaluate', { expression: 'location.href', returnByValue: true })
      .then(r => r?.result?.value)
      .catch(() => lastHref);
    lastPath = await call('Runtime.evaluate', { expression: 'location.pathname', returnByValue: true })
      .then(r => r?.result?.value)
      .catch(() => lastPath);

    const okPath = WAIT_PATH_PREFIX ? String(lastPath).startsWith(WAIT_PATH_PREFIX) : true;
    const okHref = WAIT_INCLUDES ? String(lastHref).includes(WAIT_INCLUDES) : true;
    if (okPath && okHref) return { href: lastHref, path: lastPath, lastHref, lastPath };

    await delay(500);
  }
  return { href: null, path: null, lastHref, lastPath };
}

async function main() {
  const wsUrl = await getBrowserWsUrl();
  const cdp = new CDP(wsUrl);
  await cdp.open();
  try {
    await cdp.call('Target.activateTarget', { targetId: TARGET_ID }).catch(() => null);
    const attach = await cdp.call('Target.attachToTarget', { targetId: TARGET_ID, flatten: true });
    const sessionId = attach?.sessionId;
    if (!sessionId) throw new Error('No sessionId from attachToTarget');
    const call = (m, p={}) => cdp.call(m, p, sessionId);

    await call('Page.enable');
    await call('Runtime.enable');
    await call('Page.navigate', { url: URL });

    const r = await waitForOk(call);
    if (!r.href) throw new Error(`timeout waiting for path_prefix=${WAIT_PATH_PREFIX} includes=${WAIT_INCLUDES}; lastPath=${r.lastPath}; lastHref=${r.lastHref}`);
    console.log('CDP_NAV_OK', r.path, r.href);
  } finally {
    cdp.close();
  }
}

main().catch((e) => {
  console.error('CDP_NAV_FAIL', e?.stack || e);
  process.exit(1);
});
