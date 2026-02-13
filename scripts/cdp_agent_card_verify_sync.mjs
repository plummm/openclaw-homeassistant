#!/usr/bin/env node
import { setTimeout as delay } from 'node:timers/promises';
import { writeFile } from 'node:fs/promises';

const PORT = Number(process.env.CDP_PORT || '9222');
const TARGET_ID = process.env.CDP_TARGET_ID || '';
const SHOT_PREFIX = process.env.AGENT_SYNC_SHOT_PREFIX || 'out/prod-agent-sync';
const WAIT_MS = Number(process.env.AGENT_SYNC_WAIT_MS || '22000');
const TARGET_MOOD = String(process.env.AGENT_SYNC_TARGET_MOOD || '').toLowerCase();
const TARGET_DESC_TOKEN = String(process.env.AGENT_SYNC_TARGET_DESC_TOKEN || '').toLowerCase();

if (!TARGET_ID) {
  console.error('AGENT_SYNC_FAIL missing CDP_TARGET_ID');
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
    this.handlers = new Map();
  }
  on(method, fn) { this.handlers.set(method, fn); }
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
        if (data.error) reject(new Error(data.error.message || JSON.stringify(data.error)));
        else resolve(data.result);
        return;
      }
      if (data.method && this.handlers.has(data.method)) {
        try { this.handlers.get(data.method)(data.params || {}, data.sessionId); } catch {}
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

const HELPERS = `(() => {
  function* deepNodes(root) {
    if (!root) return;
    const stack = [root];
    while (stack.length) {
      const node = stack.pop();
      if (!node) continue;
      yield node;
      if (node.shadowRoot) stack.push(node.shadowRoot);
      if (node.tagName && node.tagName.toLowerCase() === 'iframe') {
        try { if (node.contentDocument) stack.push(node.contentDocument); } catch {}
      }
      const kids = node.children ? Array.from(node.children) : [];
      for (let i = kids.length - 1; i >= 0; i--) stack.push(kids[i]);
    }
  }

  function queryDeep(selector) {
    for (const n of deepNodes(document)) {
      if (!n || typeof n.matches !== 'function') continue;
      try { if (n.matches(selector)) return n; } catch {}
    }
    return null;
  }

  function clickSelector(selector) {
    const el = queryDeep(selector);
    if (!el) return false;
    try { el.scrollIntoView({ block: 'center', inline: 'center' }); } catch {}
    try { el.focus && el.focus(); } catch {}
    try { el.click(); return true; } catch { return false; }
  }

  function textOf(selector) {
    const el = queryDeep(selector);
    return el ? String(el.textContent || '').trim() : '';
  }

  return {
    clickSelector,
    snapshot: () => ({
      mood: textOf('#agentMood'),
      desc: textOf('#agentDesc'),
      meta: textOf('#agentMeta'),
      live: textOf('#agentLiveMeta')
    })
  };
})()`;

async function main() {
  const wsUrl = await getBrowserWsUrl();
  const cdp = new CDP(wsUrl);
  await cdp.open();

  const consoleErrors = [];
  const runtimeExceptions = [];

  try {
    await cdp.call('Target.activateTarget', { targetId: TARGET_ID }).catch(() => null);
    const attach = await cdp.call('Target.attachToTarget', { targetId: TARGET_ID, flatten: true });
    const sessionId = attach?.sessionId;
    if (!sessionId) throw new Error('No sessionId');
    const call = (m, p = {}) => cdp.call(m, p, sessionId);

    cdp.on('Runtime.consoleAPICalled', (params) => {
      const type = String(params?.type || 'log');
      const args = (params?.args || []).map((a) => {
        if (Object.prototype.hasOwnProperty.call(a, 'value')) return String(a.value);
        return String(a?.description || '');
      }).join(' ');
      if (['error', 'warning'].includes(type)) consoleErrors.push({ type, text: args.slice(0, 300) });
    });

    cdp.on('Runtime.exceptionThrown', (params) => {
      const d = params?.exceptionDetails || {};
      runtimeExceptions.push({ text: String(d?.text || ''), url: String(d?.url || ''), lineNumber: d?.lineNumber });
    });

    await call('Runtime.enable');
    await call('Page.enable');

    const evalJS = async (expression) => {
      const r = await call('Runtime.evaluate', { expression, returnByValue: true });
      return r?.result?.value;
    };

    const shot = async (name) => {
      const r = await call('Page.captureScreenshot', { format: 'png' });
      const path = `${SHOT_PREFIX}-${name}.png`;
      await writeFile(path, Buffer.from(r.data, 'base64'));
      return path;
    };

    await evalJS(`${HELPERS}.clickSelector('#tabAgent')`);
    await delay(1200);

    const before = await evalJS(`${HELPERS}.snapshot()`);
    const beforeShot = await shot('before');

    const started = Date.now();
    let latest = before;
    let synced = false;

    while ((Date.now() - started) < WAIT_MS) {
      latest = await evalJS(`${HELPERS}.snapshot()`);
      const moodOk = TARGET_MOOD ? String(latest.mood || '').toLowerCase().includes(TARGET_MOOD) : true;
      const descOk = TARGET_DESC_TOKEN ? String(latest.desc || '').toLowerCase().includes(TARGET_DESC_TOKEN) : true;
      if (moodOk && descOk) {
        synced = true;
        break;
      }
      await delay(1000);
    }

    const afterShot = await shot('after');

    const out = {
      pass: synced,
      waitedMs: Date.now() - started,
      target: { mood: TARGET_MOOD, descToken: TARGET_DESC_TOKEN },
      before,
      after: latest,
      screenshots: { before: beforeShot, after: afterShot },
      consoleErrorCount: consoleErrors.length,
      runtimeExceptionCount: runtimeExceptions.length,
      consoleErrors,
      runtimeExceptions,
    };

    console.log('AGENT_SYNC_RESULT', JSON.stringify(out, null, 2));
    if (!synced) process.exitCode = 1;
    if (consoleErrors.length || runtimeExceptions.length) process.exitCode = 1;
  } finally {
    cdp.close();
  }
}

main().catch((e) => {
  console.error('AGENT_SYNC_FAIL', e?.stack || e);
  process.exit(1);
});
