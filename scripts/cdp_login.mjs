#!/usr/bin/env node
// CDP login driver for Home Assistant.
// Deterministic: creates a fresh page target, attaches, fills login (shadow DOM),
// then waits (in the same session) for location.href to include /lovelace/.

import { setTimeout as delay } from 'node:timers/promises';

const BASE = process.env.HA_BASE || 'http://100.96.0.2:8123';
const USER = process.env.HA_USER || 'test';
const PASS = process.env.HA_PASS || '12345';
const PORT = Number(process.env.CDP_PORT || '9222');
const TIMEOUT_MS = Number(process.env.CDP_TIMEOUT_MS || '60000');

async function fetchJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
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
        } else {
          resolve(data.result);
        }
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

async function waitFor(fn, { timeoutMs = 30000, intervalMs = 300 } = {}) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const v = await fn();
    if (v) return v;
    await delay(intervalMs);
  }
  return null;
}

const DEEP_FIND_HELPER = `(() => {
  const deepFind = (root, pred) => {
    const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (const el of all) {
      if (pred(el)) return el;
      if (el.shadowRoot) {
        const hit = deepFind(el.shadowRoot, pred);
        if (hit) return hit;
      }
    }
    return null;
  };
  return { deepFind };
})()`;

async function main() {
  const wsUrl = await getBrowserWsUrl();
  const cdp = new CDP(wsUrl);
  await cdp.open();

  try {
    // Create a fresh tab at BASE
    const created = await cdp.call('Target.createTarget', { url: `${BASE}/` });
    const targetId = created?.targetId;
    if (!targetId) throw new Error('Target.createTarget returned no targetId');
    if (process.env.CDP_TARGET_FILE) {
      try {
        await (await import('node:fs')).promises.writeFile(process.env.CDP_TARGET_FILE, targetId, 'utf8');
      } catch {}
    }

    const attach = await cdp.call('Target.attachToTarget', { targetId, flatten: true });
    const sessionId = attach?.sessionId;
    if (!sessionId) throw new Error('No sessionId from attachToTarget');

    const call = (method, params = {}) => cdp.call(method, params, sessionId);

    await call('Page.enable');
    await call('Runtime.enable');

    // Wait for document + auth UI load
    await waitFor(async () => {
      const r = await call('Runtime.evaluate', { expression: 'document.readyState', returnByValue: true });
      return ['interactive', 'complete'].includes(r?.result?.value);
    }, { timeoutMs: 20000, intervalMs: 500 });

    // If already logged in, just ensure we land on /lovelace
    const href0 = await call('Runtime.evaluate', { expression: 'location.href', returnByValue: true }).then(r => r?.result?.value);
    if (String(href0).includes('/lovelace/')) {
      console.log('CDP_LOGIN_OK already_on_lovelace', href0);
      return;
    }

    // Wait for username/password inputs (shadow DOM)
    const inputsReady = await waitFor(async () => {
      const r = await call('Runtime.evaluate', {
        expression: `(() => {
          const { deepFind } = ${DEEP_FIND_HELPER};
          const u = deepFind(document, (el) => el.tagName === 'INPUT' && (el.name === 'username' || el.id === 'username' || el.autocomplete === 'username'))
            || deepFind(document, (el) => el.tagName === 'INPUT' && (el.type === 'text' || el.type === 'email'));
          const p = deepFind(document, (el) => el.tagName === 'INPUT' && (el.name === 'password' || el.id === 'password' || el.autocomplete === 'current-password' || el.type === 'password'));
          return !!(u && p);
        })()`,
        returnByValue: true,
      });
      return r?.result?.value;
    }, { timeoutMs: 30000, intervalMs: 500 });

    if (!inputsReady) {
      const href = await call('Runtime.evaluate', { expression: 'location.href', returnByValue: true }).then(r => r?.result?.value);
      throw new Error(`Inputs not found; href=${href}`);
    }

    // Fill + submit (retry)
    let submitVia = null;
    for (let i = 0; i < 4; i++) {
      const res = await call('Runtime.evaluate', {
        expression: `(() => {
          const { deepFind } = ${DEEP_FIND_HELPER};
          const u = deepFind(document, (el) => el.tagName === 'INPUT' && (el.name === 'username' || el.id === 'username' || el.autocomplete === 'username'))
            || deepFind(document, (el) => el.tagName === 'INPUT' && (el.type === 'text' || el.type === 'email'));
          const p = deepFind(document, (el) => el.tagName === 'INPUT' && (el.name === 'password' || el.id === 'password' || el.autocomplete === 'current-password' || el.type === 'password'));
          if (!u || !p) return { ok:false, reason:'missing_inputs' };

          // Try to find and click "Keep me logged in"
          const keep = deepFind(document, (el) => el.tagName === 'INPUT' && el.type === 'checkbox');
          if (keep && !keep.checked) keep.click();

          u.focus(); u.value = ${JSON.stringify(USER)}; u.dispatchEvent(new Event('input', { bubbles:true })); u.dispatchEvent(new Event('change', { bubbles:true }));
          p.focus(); p.value = ${JSON.stringify(PASS)}; p.dispatchEvent(new Event('input', { bubbles:true })); p.dispatchEvent(new Event('change', { bubbles:true }));

          const root = p.getRootNode();
          const btn = (root.querySelectorAll ? Array.from(root.querySelectorAll('button, mwc-button, ha-button, input[type=submit]')) : [])
            .find(el => {
              const t = (el.innerText || el.value || el.label || '').toLowerCase();
              return t.includes('log in') || t.includes('login') || t.includes('sign in');
            });
          if (btn) { btn.click(); return { ok:true, via:'button_click' }; }

          // enter key fallback
          p.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }));
          p.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }));

          const form = u.form || p.form || deepFind(document, (el) => el.tagName === 'FORM');
          if (form?.requestSubmit) { form.requestSubmit(); return { ok:true, via:'requestSubmit' }; }
          if (form?.submit) { form.submit(); return { ok:true, via:'submit' }; }
          return { ok:false, reason:'missing_submit' };
        })()`,
        returnByValue: true,
      });

      const v = res?.result?.value;
      if (v?.ok) { submitVia = v.via; break; }
      await delay(700);
    }

    if (!submitVia) throw new Error('Submit failed');

    // Gate: same-session poll on location.href
    // Wait for at least 3 consecutive checks to be /lovelace to confirm stability.
    let stableCount = 0;
    const ok = await waitFor(async () => {
      const p = await call('Runtime.evaluate', { expression: 'location.pathname', returnByValue: true })
        .then(r => r?.result?.value)
        .catch(() => '');
      if (String(p).startsWith('/lovelace')) {
        stableCount++;
        if (stableCount >= 3) return p;
      } else {
        stableCount = 0;
      }
      return null;
    }, { timeoutMs: TIMEOUT_MS, intervalMs: 1000 });

    if (!ok) {
      const href = await call('Runtime.evaluate', { expression: 'location.href', returnByValue: true }).then(r => r?.result?.value);
      const path = await call('Runtime.evaluate', { expression: 'location.pathname', returnByValue: true }).then(r => r?.result?.value);
      throw new Error(`Gate failed; path=${path}; href=${href}`);
    }

    console.log('CDP_LOGIN_OK', submitVia, ok);
  } finally {
    cdp.close();
  }
}

main().catch((e) => {
  console.error('CDP_LOGIN_FAIL', e?.stack || e);
  process.exit(1);
});
