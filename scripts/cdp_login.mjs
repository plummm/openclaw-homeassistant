#!/usr/bin/env node
// CDP login submitter for HA (shadow DOM aware).
// Uses browser WS + attachToTarget. Does NOT wait for post-login navigation;
// caller should gate by polling /json/list for URL/path.

const BASE = process.env.HA_BASE || 'http://100.96.0.2:8123';
const USER = process.env.HA_USER || 'test';
const PASS = process.env.HA_PASS || '12345';
const PORT = Number(process.env.CDP_PORT || '9222');

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
          // Navigation can detach an attached target mid-flight; treat as soft error.
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
    const targets = await cdp.call('Target.getTargets');
    const page = targets?.targetInfos?.find((t) => t.type === 'page') || targets?.targetInfos?.[0];
    if (!page?.targetId) throw new Error('No page target found');

    const attach = await cdp.call('Target.attachToTarget', { targetId: page.targetId, flatten: true });
    const sessionId = attach?.sessionId;
    if (!sessionId) throw new Error('No sessionId from attachToTarget');

    const call = (method, params = {}) => cdp.call(method, params, sessionId);

    await call('Page.enable');
    await call('Runtime.enable');

    await call('Page.navigate', { url: `${BASE}/` });

    // Fill and submit (best-effort). Navigation can detach the target; retry a few times.
    let v = null;
    for (let i = 0; i < 4; i++) {
      const fillRes = await call('Runtime.evaluate', {
        expression: `(() => {
          const { deepFind } = ${DEEP_FIND_HELPER};
          const u = deepFind(document, (el) => el.tagName === 'INPUT' && (el.name === 'username' || el.id === 'username' || el.autocomplete === 'username'))
            || deepFind(document, (el) => el.tagName === 'INPUT' && (el.type === 'text' || el.type === 'email'));
          const p = deepFind(document, (el) => el.tagName === 'INPUT' && (el.name === 'password' || el.id === 'password' || el.autocomplete === 'current-password' || el.type === 'password'));
          if (!u || !p) return { ok:false, reason:'missing_inputs' };
          
          u.focus(); u.value = ${JSON.stringify(USER)}; 
          u.dispatchEvent(new Event('input', { bubbles:true })); 
          u.dispatchEvent(new Event('change', { bubbles:true }));
          
          p.focus(); p.value = ${JSON.stringify(PASS)}; 
          p.dispatchEvent(new Event('input', { bubbles:true })); 
          p.dispatchEvent(new Event('change', { bubbles:true }));

          // Strategy 1: Find the submit button in the same shadow scope
          // HA login form usually has inputs and button in same shadow root.
          const root = p.getRootNode();
          const btn = (root.querySelectorAll ? Array.from(root.querySelectorAll('button, mwc-button, ha-button, input[type=submit]')) : [])
            .find(el => {
               const t = (el.innerText || el.value || el.label || '').toLowerCase();
               return t.includes('log in') || t.includes('login') || t.includes('sign in');
            });

          if (btn) {
            btn.click();
            return { ok:true, via:'button_click' };
          }

          // Strategy 2: Dispatch Enter key on password
          p.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }));
          p.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }));
          
          // Strategy 3: requestSubmit on form
          const form = u.form || p.form || deepFind(document, (el) => el.tagName === 'FORM');
          if (form?.requestSubmit) { 
            form.requestSubmit(); 
            return { ok:true, via:'requestSubmit' }; 
          }
          
          return { ok:false, reason:'missing_button' };
        })()`,
        awaitPromise: true,
        returnByValue: true,
      });

      v = fillRes?.result?.value || null;
      if (v?.ok) break;
      // brief wait and retry
      await new Promise(r => setTimeout(r, 1000));
    }

    if (!v?.ok) throw new Error(`Fill failed: ${JSON.stringify(v)}`);

    console.log('CDP_LOGIN_SUBMITTED', v.via);
  } finally {
    cdp.close();
  }
}

main().catch((e) => {
  console.error('CDP_LOGIN_FAIL', e?.stack || e);
  process.exit(1);
});
