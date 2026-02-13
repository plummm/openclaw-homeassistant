#!/usr/bin/env node
import { setTimeout as delay } from 'node:timers/promises';
import { writeFile } from 'node:fs/promises';

const PORT = Number(process.env.CDP_PORT || '9222');
const TARGET_ID = process.env.CDP_TARGET_ID || '';
const TIMEOUT_MS = Number(process.env.CDP_TIMEOUT_MS || '60000');
const SHOT_PREFIX = process.env.FEATURE_VERIFY_SHOT_PREFIX || 'out/prod-feature-verify';

if (!TARGET_ID) {
  console.error('FEATURE_VERIFY_FAIL missing CDP_TARGET_ID');
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

  function visible(el) {
    if (!el) return false;
    let cur = el;
    while (cur) {
      if (cur.classList && cur.classList.contains('hidden')) return false;
      const doc = cur.ownerDocument || document;
      const win = doc.defaultView || window;
      const st = win.getComputedStyle(cur);
      if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
      cur = cur.parentElement || null;
    }
    const r = el.getBoundingClientRect ? el.getBoundingClientRect() : { width: 0, height: 0 };
    return r.width > 0 && r.height > 0;
  }

  function getSelectOptionsCount(selector) {
    const el = queryDeep(selector);
    return (el && el.options) ? el.options.length : 0;
  }

  function checkState() {
    const chatBox = queryDeep('#chatVoiceBox');
    const picker = queryDeep('#pickerModal');
    const listenBtn = queryDeep('#btnListen');
    return {
      voiceBoxVisible: visible(chatBox),
      pickerVisible: visible(picker),
      listenBtnVisible: visible(listenBtn),
      chatSessionCount: getSelectOptionsCount('#chatSessionSelect'),
      themeCount: getSelectOptionsCount('#themePreset')
    };
  }

  return { clickSelector, checkState };
})()`;

async function main() {
  const wsUrl = await getBrowserWsUrl();
  const cdp = new CDP(wsUrl);
  await cdp.open();

  try {
    await cdp.call('Target.activateTarget', { targetId: TARGET_ID }).catch(() => null);
    const attach = await cdp.call('Target.attachToTarget', { targetId: TARGET_ID, flatten: true });
    const sessionId = attach?.sessionId;
    if (!sessionId) throw new Error('No sessionId');
    const call = (m, p = {}) => cdp.call(m, p, sessionId);

    await call('Runtime.enable');
    await call('Page.enable');

    const evalJS = async (expr) => {
      const r = await call('Runtime.evaluate', { expression: expr, returnByValue: true });
      if (r?.exceptionDetails) throw new Error(r.exceptionDetails.text);
      return r?.result?.value;
    };

    const shot = async (name) => {
      const r = await call('Page.captureScreenshot', { format: 'png' });
      if (!r?.data) throw new Error(`captureScreenshot failed for ${name}`);
      const path = `${SHOT_PREFIX}-${name}.png`;
      await writeFile(path, Buffer.from(r.data, 'base64'));
      return path;
    };

    const results = { checks: [], pass: false };

    // 1. Check Listen Button (Agent Tab)
    console.log('Step 1: Check Agent tab Listen button');
    await evalJS(`${HELPERS}.clickSelector('#tabAgent')`);
    await delay(1000);
    let state = await evalJS(`${HELPERS}.checkState()`);
    results.checks.push({ name: 'listen_btn', value: state.listenBtnVisible, pass: state.listenBtnVisible });
    await shot('01-agent-tab');

    // 2. Check Chat Sessions & Voice
    console.log('Step 2: Check Chat tab sessions & voice');
    await evalJS(`${HELPERS}.clickSelector('#tabChat')`);
    await delay(2000); // allow fetch
    state = await evalJS(`${HELPERS}.checkState()`);
    results.checks.push({ name: 'chat_sessions_count', value: state.chatSessionCount, pass: state.chatSessionCount > 1 });
    
    await evalJS(`${HELPERS}.clickSelector('#chatModeVoice')`);
    await delay(1000);
    state = await evalJS(`${HELPERS}.checkState()`);
    results.checks.push({ name: 'voice_box_visible', value: state.voiceBoxVisible, pass: state.voiceBoxVisible });
    await shot('02-chat-voice');

    // 3. Check Setup Themes & Picker
    console.log('Step 3: Check Setup themes & picker');
    await evalJS(`${HELPERS}.clickSelector('#tabSetup')`);
    await delay(1000);
    state = await evalJS(`${HELPERS}.checkState()`);
    results.checks.push({ name: 'theme_count', value: state.themeCount, pass: state.themeCount > 3 });

    await evalJS(`${HELPERS}.clickSelector('button[data-pick="soc"]')`);
    await delay(1000);
    state = await evalJS(`${HELPERS}.checkState()`);
    results.checks.push({ name: 'picker_visible', value: state.pickerVisible, pass: state.pickerVisible });
    await shot('03-setup-picker');
    await evalJS(`${HELPERS}.clickSelector('#pickerClose')`);

    // 4. Check Automations Tab (Clickability)
    console.log('Step 4: Check Automations tab');
    await evalJS(`${HELPERS}.clickSelector('#tabAutomations')`);
    await delay(1000);
    // Just verify we switched (by checking active class or something, here assume click worked if no error)
    await shot('04-automations-tab');

    results.pass = results.checks.every(c => c.pass);
    console.log('FEATURE_VERIFY_RESULT', JSON.stringify(results, null, 2));
    
    if (!results.pass) process.exitCode = 1;

  } finally {
    cdp.close();
  }
}

main().catch((e) => {
  console.error('FEATURE_VERIFY_FAIL', e?.stack || e);
  process.exit(1);
});
