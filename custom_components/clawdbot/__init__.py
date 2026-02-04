"""Clawdbot custom integration.

MVP scope:
- Sidebar iframe panel (points at a Clawdbot-served mini page).
- HA services that let Home Assistant:
  - send a message to Xiaochen via Clawdbot gateway (Telegram) (clawdbot.send_chat)
  - call Clawdbot gateway /tools/invoke for arbitrary tools (clawdbot.tools_invoke)
  - control Home Assistant itself via Clawdbot's Home Assistant MCP server (clawdbot.ha_get_states, clawdbot.ha_call_service)

Config (configuration.yaml):

clawdbot:
  url: "http://host.docker.internal:7773/__clawdbot__/canvas/ha-panel/"   # panel URL
  token: "<gateway-token>"                                              # gateway.auth.token
  session_key: "main"

Notes:
- `sessions_send` via gateway /tools/invoke hangs in current Clawdbot build; use message.send for MVP.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp

from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.storage import Store
from homeassistant.exceptions import HomeAssistantError

DOMAIN = "clawdbot"
_LOGGER = logging.getLogger(__name__)

DEFAULT_TITLE = "Clawdbot"
DEFAULT_ICON = "mdi:robot"

# HA-embedded panel is served by Home Assistant itself.
# This avoids OpenClaw Control UI "device identity" requirements and mixed-content issues.
# NOTE: Avoid /api/* paths: Home Assistant treats them as authenticated API endpoints
# and will return 401/ban unauthenticated iframe navigations.
PANEL_PATH = "/clawdbot-panel"

DEFAULT_URL = PANEL_PATH  # legacy/override name; now defaults to HA-local panel
DEFAULT_SESSION_KEY = "main"

CONF_URL = "url"  # legacy
CONF_PANEL_URL = "panel_url"  # preferred
CONF_TOKEN = "token"
CONF_SESSION_KEY = "session_key"
CONF_GATEWAY_URL = "gateway_url"

SERVICE_SEND_CHAT = "send_chat"
SERVICE_TOOLS_INVOKE = "tools_invoke"
SERVICE_HA_GET_STATES = "ha_get_states"
SERVICE_HA_CALL_SERVICE = "ha_call_service"
SERVICE_GATEWAY_TEST = "gateway_test"
SERVICE_SET_MAPPING = "set_mapping"
SERVICE_REFRESH_HOUSE_MEMORY = "refresh_house_memory"
SERVICE_NOTIFY_EVENT = "notify_event"
SERVICE_CHAT_FETCH = "chat_fetch"


async def _gw_post(session: aiohttp.ClientSession, url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as r:
        txt = await r.text()
        if r.status >= 400:
            raise RuntimeError(f"Gateway HTTP {r.status}: {txt}")
        try:
            return await r.json()
        except Exception:
            return {"raw": txt}


def _derive_gateway_origin(panel_url: str) -> str:
    try:
        from urllib.parse import urlparse

        u = urlparse(panel_url)
        if u.scheme and u.netloc:
            return f"{u.scheme}://{u.netloc}"
    except Exception:
        pass
    return panel_url


MAPPING_STORE_KEY = "clawdbot_mapping"
MAPPING_STORE_VERSION = 1
HOUSEMEM_STORE_KEY = "clawdbot_house_memory"
HOUSEMEM_STORE_VERSION = 1
CHAT_STORE_KEY = "clawdbot_chat_history"
CHAT_STORE_VERSION = 1

PANEL_HTML = """<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\"/>
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>
  <meta http-equiv=\"Cache-Control\" content=\"no-store\"/>
  <meta http-equiv=\"Pragma\" content=\"no-cache\"/>
  <meta http-equiv=\"Expires\" content=\"0\"/>
  <title>Clawdbot</title>
  <style>
    html{background:var(--primary-background-color);}
    body{font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;padding:16px;max-width:980px;margin:0 auto;
      background:linear-gradient(180deg,
        color-mix(in srgb, var(--mdc-theme-primary, var(--primary-color)) 14%, var(--primary-background-color)) 0%,
        var(--primary-background-color) 220px);
      color:var(--primary-text-color);
    }
    input,button,textarea{font:inherit;}
    input,textarea{
      width:100%;
      height:44px;
      padding:0 14px;
      border-radius:12px;
      border:1px solid color-mix(in srgb, var(--divider-color) 78%, #000 14%);
      background:color-mix(in srgb, var(--ha-card-background, var(--card-background-color)) 72%, transparent);
      color:var(--primary-text-color);
      outline:none;
    }
    textarea{height:auto;padding:12px 14px;}
    input:focus,textarea:focus{
      border-color:var(--mdc-theme-primary, var(--primary-color));
      box-shadow:0 0 0 3px color-mix(in srgb, var(--mdc-theme-primary, var(--primary-color)) 22%, transparent);
    }
    code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:12px;}
    .surface{
      background:linear-gradient(180deg,
        color-mix(in srgb, var(--mdc-theme-primary, var(--primary-color)) 22%, var(--primary-background-color)) 0%,
        color-mix(in srgb, var(--mdc-theme-primary, var(--primary-color)) 10%, var(--primary-background-color)) 240px,
        var(--primary-background-color) 560px);
      border-radius:16px;
      padding:18px;
      border:1px solid color-mix(in srgb, var(--divider-color) 88%, #000 12%);
      box-shadow:0 10px 34px rgba(0,0,0,.08);
    }
    .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;}
    .card{border:1px solid color-mix(in srgb, var(--divider-color) 82%, #000 10%);border-radius:16px;padding:16px;margin:14px 0;
      background:color-mix(in srgb, var(--ha-card-background, var(--card-background-color)) 94%, transparent);
      box-shadow:0 8px 20px rgba(0,0,0,.07);
      backdrop-filter:saturate(1.1);
    }
    .muted{color:var(--secondary-text-color);font-size:13px;}
    .ok{color:#0a7a2f;}
    .bad{color:#a00000;}
    .btn{height:44px;padding:0 14px;border:1px solid var(--divider-color);border-radius:12px;background:var(--secondary-background-color);color:var(--primary-text-color);cursor:pointer;display:inline-flex;align-items:center;justify-content:center;gap:8px;}
    .btn:hover{filter:brightness(0.98);}
    .btn:disabled{opacity:0.5;cursor:not-allowed;filter:none;}
    .btn.primary{border-color:var(--mdc-theme-primary, var(--primary-color));background:var(--mdc-theme-primary, var(--primary-color));color:var(--text-primary-color, #fff);}
    .btn.primary:hover{filter:brightness(0.95);}
    .tabs{display:flex;gap:10px;margin-top:10px;margin-bottom:12px;}
    .tab{height:40px;padding:0 14px;border:1px solid color-mix(in srgb, var(--divider-color) 95%, transparent);border-radius:999px;
      background:transparent;
      color:var(--primary-text-color);opacity:0.92;cursor:pointer;display:inline-flex;align-items:center;}
    .tab:hover{background:color-mix(in srgb, var(--ha-card-background, var(--card-background-color)) 55%, transparent);}
    .tab.active{opacity:1;background:var(--mdc-theme-primary, var(--primary-color));border-color:var(--mdc-theme-primary, var(--primary-color));color:#fff;font-weight:800;
      box-shadow:0 8px 22px color-mix(in srgb, var(--mdc-theme-primary, var(--primary-color)) 30%, transparent);}
    .hidden{display:none;}
    .kv{display:flex;gap:10px;flex-wrap:wrap;margin-top:8px;}
    .kv > div{background:var(--secondary-background-color);border:1px solid var(--divider-color);border-radius:10px;padding:8px 10px;}
    .pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;border:1px solid var(--divider-color);background:var(--secondary-background-color);color:#374151;}
    .pill.ok{border-color:var(--success-color, #2e7d32);background:color-mix(in srgb, var(--success-color, #2e7d32) 15%, transparent);color:var(--success-color, #2e7d32);}
    .pill.bad{border-color:var(--error-color, #b00020);background:color-mix(in srgb, var(--error-color, #b00020) 15%, transparent);color:var(--error-color, #b00020);}
    .entities{max-height:420px;overflow:auto;border:1px solid var(--divider-color);border-radius:8px;padding:8px;}
    .grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;}
    .setup-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;align-items:start;}
    @media (max-width: 860px){ .setup-grid{grid-template-columns:1fr;} }
    .ent{display:flex;gap:10px;align-items:center;justify-content:space-between;border-bottom:1px solid #f0f0f0;padding:6px 0;}
    .ent:last-child{border-bottom:none;}
    .ent-id{font-weight:600;}
    .ent-state{color:#444;}
    .suggest-card{border:1px solid #eef2f7;border-radius:12px;padding:10px;background:var(--secondary-background-color);}
    .choice{display:flex;gap:8px;align-items:flex-start;padding:4px 0;}
    .choice input{margin-top:3px;}
    .choice-main{font-size:13px;}
    .choice-meta{font-size:12px;color:var(--secondary-text-color);}
    .chat-shell{display:flex;flex-direction:column;height:min(68vh,720px);min-height:0;border:1px solid var(--divider-color);border-radius:16px;background:color-mix(in srgb, var(--ha-card-background, var(--card-background-color)) 92%, transparent);box-shadow:0 6px 18px rgba(0,0,0,.06);overflow:hidden;}
    .chat-list{flex:1;min-height:0;overflow:auto;padding:0 16px 16px 16px;position:relative;background:linear-gradient(180deg, color-mix(in srgb, var(--secondary-background-color) 90%, transparent) 0%, transparent 65%);} 
    .chat-stack{display:flex;flex-direction:column;gap:12px;min-height:100%;justify-content:flex-end;}
    .chat-row{display:flex;align-items:flex-end;gap:10px;}
    .chat-row.user{justify-content:flex-end;}
    .chat-row.agent{justify-content:flex-start;}
    .chat-bubble{max-width:72%;padding:12px 14px;border-radius:16px;border:1px solid color-mix(in srgb, var(--divider-color) 75%, transparent);background:var(--secondary-background-color);box-shadow:0 6px 14px rgba(0,0,0,.04);white-space:pre-wrap;}
    .chat-row.user .chat-bubble{background:var(--mdc-theme-primary, var(--primary-color));border-color:var(--mdc-theme-primary, var(--primary-color));color:#fff;}
    .chat-row.agent .chat-bubble{background:var(--ha-card-background, var(--card-background-color));border-color:color-mix(in srgb, var(--divider-color) 95%, transparent);color:var(--primary-text-color);}
    .chat-meta{font-size:12px;color:var(--secondary-text-color);margin-top:6px;display:flex;gap:8px;align-items:center;justify-content:space-between;}
    .chat-input{display:flex;gap:10px;padding:12px;border-top:1px solid var(--divider-color);background:color-mix(in srgb, var(--secondary-background-color) 92%, transparent);box-shadow:0 -10px 30px rgba(0,0,0,.06);}
    .chat-input input{flex:1;min-width:220px;height:46px;}
    .chat-bubble pre{margin:8px 0 0 0;padding:10px 12px;border-radius:12px;background:color-mix(in srgb, var(--primary-background-color) 65%, transparent);border:1px solid color-mix(in srgb, var(--divider-color) 80%, transparent);overflow:auto;}
    .chat-bubble code{background:color-mix(in srgb, var(--primary-background-color) 70%, transparent);padding:2px 6px;border-radius:8px;}
    .chat-load{position:sticky;top:0;z-index:2;display:flex;justify-content:center;margin:0;padding:0;
      background:linear-gradient(180deg, color-mix(in srgb, var(--ha-card-background, var(--card-background-color)) 92%, transparent) 0%, transparent 100%);
    }
    .chat-load .btn{height:32px;font-size:12px;padding:0 12px;border-radius:999px;margin:0;background:color-mix(in srgb, var(--ha-card-background, var(--card-background-color)) 70%, transparent);}
    @media (max-width: 680px){ .chat-bubble{max-width:90%;} .chat-shell{height:72vh;} }
  </style>
</head>
<body>
  <div class=\"surface\">
  <h1 style=\"margin:0 0 4px 0;\">Clawdbot</h1>
  <div class=\"muted\" style=\"margin:0 0 10px 0;\">Home Assistant panel (served by HA). Uses HA auth to call HA services which relay to OpenClaw.</div>

  <script>window.__CLAWDBOT_CONFIG__ = __CONFIG_JSON__;</script>

  <div class=\"tabs\">
    <button type=\"button\" class=\"tab\" id=\"tabSetup\">Setup</button>
    <button type=\"button\" class=\"tab active\" id=\"tabCockpit\">Cockpit</button>
    <button type=\"button\" class=\"tab\" id=\"tabChat\">Chat</button>
  </div>

  <div id=\"viewSetup\" class=\"hidden\">
    <div class=\"setup-grid\">
    <div class=\"card\">
      <h2>Commissioning</h2>
      <div class=\"muted\">Verify configuration and connectivity before using the cockpit.</div>
      <div class=\"kv\" id=\"cfgSummary\"></div>
      <div class=\"row\" style=\"margin-top:10px\">
        <button class=\"btn primary\" id=\"btnGatewayTest\">Test gateway</button>
        <span class=\"muted\" id=\"gwTestResult\"></span>
      </div>

      <div style=\"margin-top:14px\">
        <div class=\"muted\" style=\"margin-bottom:8px\">Send test inbound event (calls <code>clawdbot.notify_event</code>):</div>
        <div class=\"row\">
          <input id=\"evtType\" placeholder=\"event_type\" value=\"clawdbot.test\"/>
          <select id=\"evtSeverity\" style=\"height:44px;border-radius:12px;padding:0 12px;border:1px solid color-mix(in srgb, var(--divider-color) 80%, transparent);background:color-mix(in srgb, var(--ha-card-background, var(--card-background-color)) 70%, transparent);color:var(--primary-text-color);\">
            <option value=\"info\" selected>info</option>
            <option value=\"warning\">warning</option>
            <option value=\"critical\">critical</option>
          </select>
          <input id=\"evtSource\" placeholder=\"source\" value=\"panel\"/>
        </div>
        <textarea id=\"evtAttrs\" style=\"margin-top:8px\" rows=\"3\" placeholder=\"attributes JSON (optional)\"></textarea>
        <div class=\"row\" style=\"margin-top:8px\">
          <button class=\"btn\" id=\"btnSendEvent\">Send test event</button>
          <span class=\"muted\" id=\"evtResult\" style=\"min-width:180px;display:inline-block\"></span>
        </div>
      </div>
    </div>

    <div class=\"card\">
      <h2>Core signal mapping</h2>
      <div class=\"muted\">Select a suggestion per signal and confirm to save. Manual overrides remain available below.</div>
      <div class=\"grid2\" id=\"suggestions\" style=\"margin-top:10px\"></div>
      <div class=\"muted\" style=\"margin-top:10px\">Manual override (entity_id):</div>
      <div class=\"row\" style=\"margin-top:8px\">
        <input id=\"mapSoc\" style=\"flex:1;min-width:220px\" placeholder=\"soc entity_id (e.g. sensor.battery_soc)\"/>
        <input id=\"mapVoltage\" style=\"flex:1;min-width:220px\" placeholder=\"voltage entity_id\"/>
      </div>
      <div class=\"row\" style=\"margin-top:8px\">
        <input id=\"mapSolar\" style=\"flex:1;min-width:220px\" placeholder=\"solar power entity_id\"/>
        <input id=\"mapLoad\" style=\"flex:1;min-width:220px\" placeholder=\"load/consumption entity_id\"/>
      </div>
    </div>

    <div class=\"card\">
      <h2>Chat</h2>
      <div class=\"muted\">Calls HA service <code>clawdbot.send_chat</code>.</div>
      <div class=\"row\" style=\"margin-top:8px\">
        <input id=\"chatInput\" style=\"flex:1;min-width:240px\" placeholder=\"Type message…\"/>
        <button class=\"btn primary\" id=\"chatSend\">Send</button>
      </div>
      <div class=\"muted\" id=\"chatResult\" style=\"margin-top:8px\"></div>
    </div>
    </div>
  </div>

  <div id=\"viewCockpit\">
    <div class=\"card\">
      <h2>Recommendations (preview)</h2>
      <div class=\"muted\">Informational only (no alerts). Based on your mapped signals + house memory.</div>
      <div id=\"recs\" style=\"margin-top:10px\"><div class=\"muted\">No recommendations yet.</div></div>
      <div class=\"muted\" id=\"recsText\" style=\"display:none\">Finish mapping core signals</div>
    </div>

    <div class=\"card\">
      <h2>House memory</h2>
      <div class=\"muted\">Derived from entities (heuristics). Read-only for now.</div>
      <div id=\"houseMemory\" style=\"margin-top:10px\">
        <ul style=\"margin:0;padding-left:18px\">
          <li><b>Solar:</b> …</li>
          <li><b>Battery:</b> …</li>
          <li><b>Grid:</b> …</li>
          <li><b>Generator:</b> …</li>
        </ul>
      </div>
    </div>

    <div class=\"card\">
      <h2>Core signals (mapped)</h2>
      <div class=\"muted\">Shows values for the configured entity mapping (or “unmapped”).</div>
      <div id=\"mappedValues\" class=\"grid2\" style=\"margin-top:10px\"></div>
    </div>

    <div class=\"card\" id=\"statusCard\">
      <div class=\"row\">
        <div class=\"row\"><div><b>Status:</b> <span id=\"status\">checking…</span></div><span id=\"connPill\" class=\"pill\">…</span></div>
        <button class=\"btn\" id=\"refreshBtn\">Refresh entities</button>
      </div>
      <div class=\"muted\" id=\"statusDetail\"></div>
    </div>

    <div class=\"card\">
      <h2>Entities (local HA)</h2>
      <div class=\"muted\">Lists entities from HA frontend state; controls call <code>clawdbot.ha_call_service</code>.</div>
      <div class=\"row\" style=\"margin-top:8px\">
        <input id=\"filter\" style=\"flex:1;min-width:240px\" placeholder=\"Filter (e.g. input_boolean, switch., light.kitchen)…\"/>
        <button class=\"btn\" id=\"clearFilter\">Clear</button>
      </div>
      <div class=\"entities\" id=\"entities\" style=\"margin-top:10px\"></div>
    </div>
  </div>

  <div id=\"viewChat\" class=\"hidden\">
    <div class=\"chat-shell\">
      <div id=\"chatList\" class=\"chat-list\"></div>
      <div class=\"chat-input\">
        <input id=\"chatComposer\" placeholder=\"Ask Clawdbot…\"/>
        <button class=\"btn primary\" id=\"chatComposerSend\" style=\"min-width:96px\">Send</button>
      </div>
    </div>
  </div>
  </div>

<script>
(function(){
  function qs(sel){ return document.querySelector(sel); }
  function setHidden(el, hidden){
    if (!el) return;
    // Use explicit display toggling to avoid any class/CSS interference.
    el.classList.toggle('hidden', !!hidden);
    el.style.display = hidden ? 'none' : '';
  }

  let chatItems = [];
  let chatHasOlder = false;
  let chatLoadingOlder = false;
  let chatSessionKey = null;
  const DEBUG_UI = (() => {
    try{
      const qs1 = new URLSearchParams(window.location.search || '');
      if (qs1.get('debug') === '1') return true;
      // If the panel is embedded at /clawdbot (iframe), the debug flag may be on the parent URL.
      const parentSearch = (window.parent && window.parent.location) ? (window.parent.location.search || '') : '';
      const qs2 = new URLSearchParams(parentSearch);
      return qs2.get('debug') === '1';
    } catch(e){
      return false;
    }
  })();

  function escapeHtml(txt){
    return String(txt)
      .replaceAll('&','&amp;')
      .replaceAll('<','&lt;')
      .replaceAll('>','&gt;');
  }

  function isAtBottom(list){
    if (!list) return true;
    const gap = list.scrollHeight - list.scrollTop - list.clientHeight;
    return gap < 6;
  }

  function renderChat(opts){
    const list = qs('#chatList');
    if (!list) return;
    const preserveScroll = !!(opts && opts.preserveScroll);
    const shouldAutoScroll = !!(opts && opts.autoScroll);
    const wasAtBottom = isAtBottom(list);
    const prevScrollHeight = list.scrollHeight;
    const prevScrollTop = list.scrollTop;
    list.innerHTML = '';

    // Load-older control must be the first child inside the scroll container.
    if (chatHasOlder || chatLoadingOlder) {
      const loadWrap = document.createElement('div');
      loadWrap.className = 'chat-load';
      const btn = document.createElement('button');
      btn.className = 'btn';
      btn.textContent = chatLoadingOlder ? 'Loading…' : 'Load older';
      btn.disabled = chatLoadingOlder;
      btn.onclick = () => { loadOlderChat(); };
      loadWrap.appendChild(btn);
      list.appendChild(loadWrap);
    }

    const stack = document.createElement('div');
    stack.className = 'chat-stack';
    list.appendChild(stack);

    if (!chatItems || !chatItems.length) {
      const empty = document.createElement('div');
      empty.className = 'muted';
      empty.style.textAlign = 'center';
      empty.style.marginTop = '18px';
      empty.textContent = 'No messages yet. Say hi.';
      stack.appendChild(empty);
      return;
    }

    for (const msg of chatItems){
      const row = document.createElement('div');
      row.className = `chat-row ${msg.role === 'user' ? 'user' : 'agent'}`;
      const bubble = document.createElement('div');
      bubble.className = 'chat-bubble';
      const parts = String(msg.text || '').split('```');
      let html = '';
      for (let i = 0; i < parts.length; i++){
        const seg = escapeHtml(parts[i]);
        if (i % 2 === 0){
          html += seg.replaceAll('\\n', '<br/>');
        } else {
          html += `<pre><code>${seg}</code></pre>`;
        }
      }
      bubble.innerHTML = html;
      const meta = document.createElement('div');
      meta.className = 'chat-meta';
      meta.innerHTML = `<span>${msg.role === 'user' ? 'You' : 'Clawdbot'}</span><span>${msg.ts || ''}</span>`;
      bubble.appendChild(meta);
      row.appendChild(bubble);
      stack.appendChild(row);
    }
    if (preserveScroll) {
      const nextScrollHeight = list.scrollHeight;
      list.scrollTop = nextScrollHeight - prevScrollHeight + prevScrollTop;
    } else if (shouldAutoScroll || wasAtBottom) {
      list.scrollTop = list.scrollHeight;
    }
  }

  function renderConfigSummary(){
    const cfg = (window.__CLAWDBOT_CONFIG__ || {});
    const root = qs('#cfgSummary');
    const items = [
      ['gateway_url', cfg.gateway_url || '(missing)'],
      ['token', cfg.has_token ? 'present' : 'missing'],
      ['target', cfg.target || '(missing)'],
    ];
    root.innerHTML = '';
    for (const [k,v] of items){
      const d = document.createElement('div');
      d.innerHTML = `<div class="muted">${k}</div><div><b>${String(v)}</b></div>`;
      root.appendChild(d);
    }
  }

  function loadChatFromConfig(){
    const cfg = (window.__CLAWDBOT_CONFIG__ || {});
    const items = Array.isArray(cfg.chat_history) ? cfg.chat_history : [];
    chatItems = items.map((it) => ({
      id: it.id,
      ts: it.ts,
      role: it.role,
      session_key: it.session_key,
      text: it.text,
    }));
    chatHasOlder = !!cfg.chat_history_has_older;
    chatSessionKey = cfg.session_key || cfg.target || null;

    // Dev/test toggle: enable `?debug=1` to force showing paging control for UI QA.
    if (DEBUG_UI && (chatItems && chatItems.length >= 1)) chatHasOlder = true;

  }

  async function hassFetch(path, opts){
    const parent = window.parent;
    if (!parent) throw new Error('No parent window');
    if (parent.hass && typeof parent.hass.fetchWithAuth === 'function') {
      return parent.hass.fetchWithAuth(path, opts || {});
    }
    if (parent.hass && parent.hass.connection && typeof parent.hass.connection.fetchWithAuth === 'function') {
      return parent.hass.connection.fetchWithAuth(path, opts || {});
    }
    if (parent.hass && typeof parent.hass.callApi === 'function') {
      const apiPath = String(path || '').replace(/^\\/api\\//, '');
      return parent.hass.callApi('GET', apiPath);
    }
    throw new Error('Unable to fetch with Home Assistant auth');
  }

  async function loadOlderChat(){
    if (chatLoadingOlder) return;
    const beforeId = (() => {
      for (const it of (chatItems || [])) {
        if (it && it.id) return it.id;
      }
      return null;
    })();
    if (!beforeId) {
      chatHasOlder = false;
      renderChat({ preserveScroll: true });
      return;
    }
    chatLoadingOlder = true;
    renderChat({ preserveScroll: true });
    try{
      const params = new URLSearchParams();
      params.set('limit', '50');
      params.set('before_id', beforeId);
      if (chatSessionKey) params.set('session_key', chatSessionKey);
      const resp = await hassFetch('/api/clawdbot/chat_history?' + params.toString());
      const data = resp && resp.json ? await resp.json() : resp;
      const items = (data && Array.isArray(data.items)) ? data.items : [];
      const existing = new Set((chatItems || []).map((it)=>it && it.id).filter(Boolean));
      const prepend = [];
      for (const it of items){
        if (!it || !it.id || existing.has(it.id)) continue;
        prepend.push(it);
      }
      if (prepend.length) {
        chatItems = prepend.concat(chatItems || []);
      }
      chatHasOlder = !!(data && data.has_older);
    } catch(e){
      console.warn('chat_history fetch failed', e);
    } finally {
      chatLoadingOlder = false;
      renderChat({ preserveScroll: true });
    }
  }



  function getMapping(){
    const cfg = (window.__CLAWDBOT_CONFIG__ || {});
    return cfg.mapping || {};
  }

  function fillMappingInputs(){
    const m = getMapping();
    const byId = (id)=>document.getElementById(id);
    const set = (id, val)=>{ const el=byId(id); if (el) el.value = (val || ''); };
    set('mapSoc', m.soc);
    set('mapVoltage', m.voltage);
    set('mapSolar', m.solar);
    set('mapLoad', m.load);
  }

  function setConfigMapping(next){
    if (!window.__CLAWDBOT_CONFIG__) window.__CLAWDBOT_CONFIG__ = {};
    window.__CLAWDBOT_CONFIG__.mapping = next || {};
  }

  function mappingWithDefaults(){
    const m = getMapping();
    return {
      soc: m.soc || null,
      voltage: m.voltage || null,
      solar: m.solar || null,
      load: m.load || null,
    };
  }

  function manualInputValue(field){
    const ids = {
      soc: 'mapSoc',
      voltage: 'mapVoltage',
      solar: 'mapSolar',
      load: 'mapLoad',
    };
    const el = document.getElementById(ids[field]);
    return el ? el.value.trim() : '';
  }

  async function confirmFieldMapping(field){
    const picked = document.querySelector(`input[name="sugg-${field}"]:checked`);
    const manual = manualInputValue(field);
    let value = null;
    if (picked && picked.value && picked.value !== '__manual__') {
      value = picked.value;
    } else if (manual) {
      value = manual;
    }
    const mapping = mappingWithDefaults();
    mapping[field] = value;
    const resultEl = document.getElementById(`confirm-${field}`);
    if (resultEl) resultEl.textContent = 'saving…';
    try{
      await callService('clawdbot','set_mapping',{mapping});
      setConfigMapping(mapping);
      fillMappingInputs();
      if (resultEl) resultEl.textContent = value ? 'saved' : 'cleared';
      try{ const { hass } = await getHass(); renderMappedValues(hass); renderRecommendations(hass); } catch(e){}
    } catch(e){
      if (resultEl) resultEl.textContent = 'error: ' + String(e);
    }
  }




  function renderRecommendations(hass){
    const el = document.getElementById('recs');
    if (!el) return;
    const cfg = (window.__CLAWDBOT_CONFIG__ || {});
    const mem = cfg.house_memory || {};
    const mapping = cfg.mapping || {};

    const items=[];

    const toNumber = (val) => {
      if (val === null || val === undefined) return null;
      const n = Number.parseFloat(String(val));
      return Number.isFinite(n) ? n : null;
    };
    const powerToWatts = (val, unit) => {
      if (val === null) return null;
      const u = (unit || '').toLowerCase();
      if (u === 'kw' || u === 'kilowatt' || u === 'kilowatts') return val * 1000;
      if (u === 'w' || u === 'watt' || u === 'watts') return val;
      return val;
    };

    // Estimate hours remaining (v0)
    if (mapping.soc && mapping.load) {
      let socPct = null;
      let loadW = null;
      let solarW = null;
      try{
        const socSt = hass && hass.states ? hass.states[mapping.soc] : null;
        const loadSt = hass && hass.states ? hass.states[mapping.load] : null;
        const solarSt = mapping.solar && hass && hass.states ? hass.states[mapping.solar] : null;
        socPct = toNumber(socSt ? socSt.state : null);
        if (socPct !== null && socPct <= 1) socPct = socPct * 100;
        socPct = socPct !== null ? Math.max(0, Math.min(100, socPct)) : null;
        const loadUnit = loadSt && loadSt.attributes ? loadSt.attributes.unit_of_measurement : '';
        loadW = powerToWatts(toNumber(loadSt ? loadSt.state : null), loadUnit);
        const solarUnit = solarSt && solarSt.attributes ? solarSt.attributes.unit_of_measurement : '';
        solarW = powerToWatts(toNumber(solarSt ? solarSt.state : null), solarUnit);
      } catch(e){}

      let capacityKwh = null;
      if (mem.battery && typeof mem.battery.capacity_kwh === 'number') {
        capacityKwh = mem.battery.capacity_kwh;
      } else if (mem.battery && typeof mem.battery.capacity_wh === 'number') {
        capacityKwh = mem.battery.capacity_wh / 1000;
      }
      const usedPlaceholder = !capacityKwh;
      if (!capacityKwh) capacityKwh = 10;

      // Default: still show an informational message, even if values are not numeric yet.
      let body = 'Cannot estimate yet: mapped SOC/load values are missing or non-numeric.';
      if (socPct !== null && loadW !== null && loadW > 0) {
        const availableKwh = capacityKwh * (socPct / 100);
        const hours = (availableKwh * 1000) / loadW;
        const hoursText = hours >= 1 ? `${hours.toFixed(1)} h` : `${Math.max(0, hours * 60).toFixed(0)} min`;
        body = `Estimated runtime remaining (conservative): ~${hoursText}.`;
        if (mapping.solar && solarW !== null) {
          body += ' Solar is mapped but not counted in this estimate.';
        }
      } else if (loadW !== null && loadW <= 0) {
        body = 'Cannot estimate yet: load is 0 or negative.';
      } else {
        // Give a clearer reason if entities are mapped but not numeric.
        try{
          const socSt = hass && hass.states ? hass.states[mapping.soc] : null;
          const loadSt = hass && hass.states ? hass.states[mapping.load] : null;
          // keep raw values behind a debug flag (user-facing UI should stay clean)
          const DEBUG = false;
          if (DEBUG) {
            const socRaw = socSt ? socSt.state : null;
            const loadRaw = loadSt ? loadSt.state : null;
            body += ` (soc=${socRaw}, load=${loadRaw})`;
          }
        } catch(e){}
      }
      if (usedPlaceholder) {
        body += ' Assuming 10 kWh battery capacity (placeholder).';
      }
      items.push({
        title: 'Estimate (preview): Battery hours remaining',
        body,
      });
    }

    // Basic commissioning reminder
    const missing = [];
    if (!mapping.soc) missing.push('battery SOC');
    if (!mapping.solar) missing.push('solar power');
    if (!mapping.load) missing.push('load power');
    if (missing.length) {
      items.push({
        title: 'Finish mapping core signals',
        body: `To enable better insights, map: ${missing.join(', ')}.`
      });
    }

    // Off-grid risk heuristic (placeholder)
    const solarPresent = mem.solar && mem.solar.present;
    const batteryPresent = mem.battery && mem.battery.present;
    if (batteryPresent && solarPresent) {
      items.push({
        title: 'Off-grid reserve check (preview)',
        body: 'If you are fully off-grid, watch battery SOC especially during cloudy periods. (Weather integration coming later.)'
      });
    } else if (batteryPresent && !solarPresent) {
      items.push({
        title: 'Battery present, no solar detected',
        body: 'If this is unexpected, check entity naming or map a solar/pv sensor. Otherwise plan charging accordingly.'
      });
    }

    // Weather-based preview (v0, informational only)
    try{
      let weatherId = null;
      if (hass && hass.states) {
        for (const id of Object.keys(hass.states)) {
          if (id.startsWith('weather.')) { weatherId = id; break; }
        }
      }
      if (!weatherId) {
        items.push({
          title: 'Weather (preview)',
          body: 'Not configured: add any Home Assistant weather integration (entity weather.*) to unlock cloud/rain-aware battery guidance.',
          cta: { label: 'Configure weather', href: '/config/integrations' }
        });
      } else {
        const st = hass.states[weatherId];
        const attrs = (st && st.attributes) ? st.attributes : {};
        const temp = (attrs.temperature ?? attrs.temp ?? null);
        const tempUnit = (attrs.temperature_unit ?? '°');
        const condition = st ? st.state : 'unknown';

        // Forecast timestamp (best-effort): HA weather integrations typically expose attrs.forecast[]
        // with a datetime field (datetime/time).
        let forecastAt = null;
        try{
          const fc = attrs.forecast;
          if (Array.isArray(fc) && fc.length){
            const first = fc[0] || {};
            forecastAt = first.datetime || first.time || null;
          }
        } catch(e){}

        const cond = String(condition || '').toLowerCase();
        const isBad = (cond.includes('rain') || cond.includes('pour') || cond.includes('storm') || cond.includes('snow') || cond.includes('sleet') || cond.includes('hail') || cond.includes('cloud') || cond.includes('fog'));
        const isGood = (cond.includes('clear') || cond.includes('sun') || cond.includes('partly') || cond.includes('fair'));
        let hint = '';
        if (isBad) hint = 'Expect reduced solar harvest; consider conserving load.';
        else if (isGood) hint = 'Good solar window; consider charging/deferrable loads.';

        let body = `Current: ${condition}`;
        if (temp !== null && temp !== undefined) body += `, ${temp}${tempUnit}`;
        if (forecastAt) body += `. Forecast @ ${forecastAt}`;
        body += ` (${weatherId}).`;
        if (hint) body += ` ${hint}`;
        items.push({ title: 'Weather (preview)', body, meta: weatherId });
      }
    } catch(e){}

    // If SOC mapped, show quick status line
    try{
      if (mapping.soc && hass && hass.states && hass.states[mapping.soc]){
        const st=hass.states[mapping.soc];
        const unit=(st.attributes && st.attributes.unit_of_measurement) ? (' '+st.attributes.unit_of_measurement) : '';
        items.push({title:'Current battery SOC', body: `${st.state}${unit} (${mapping.soc})`});
      }
    } catch(e){}

    if (!items.length) {
      items.push({title:'No recommendations yet', body:'Add mappings (SOC/solar/load) to unlock insights.'});
    }

    el.innerHTML = '';
    for (const it of items){
      const d=document.createElement('div');
      d.style.border='1px solid #f1f5f9';
      d.style.borderRadius='10px';
      d.style.padding='10px 12px';
      d.style.margin='8px 0';
      const meta = it.meta ? `<div class="muted" style="margin-top:4px">${it.meta}</div>` : '';
      const cta = it.cta ? `<div style="margin-top:8px"><a class="btn" href="${it.cta.href}" target="_parent">${it.cta.label}</a></div>` : '';
      d.innerHTML = `<div style="font-weight:600">${it.title}</div><div class="muted" style="margin-top:4px">${it.body}</div>${meta}${cta}`;
      el.appendChild(d);
    }
  }
  function renderHouseMemory(){
    const el = document.getElementById('houseMemory');
    if (!el) return;
    const cfg = (window.__CLAWDBOT_CONFIG__ || {});
    const mem = cfg.house_memory || {};

    const rows = [
      ['Solar', mem.solar],
      ['Battery', mem.battery],
      ['Grid', mem.grid],
      ['Generator', mem.generator],
    ];

    el.innerHTML = '';
    const ul = document.createElement('ul');
    ul.style.margin = '0';
    ul.style.paddingLeft = '18px';
    for (const [label, obj] of rows){
      const present = obj && obj.present;
      const conf = obj && (obj.confidence ?? 0);
      const li = document.createElement('li');
      li.innerHTML = `<b>${label}:</b> ${present ? 'present' : 'not detected'} <span class=\"muted\">(confidence ${Math.round((conf||0)*100)}%}</span>`;
      ul.appendChild(li);
    }
    el.appendChild(ul);
  }
  function renderMappedValues(hass){
    const root = qs('#mappedValues');
    if (!root) return;
    root.innerHTML='';

    const m = getMapping();
    const rows = [
      { key:'soc', label:'Battery SOC', unitLabel:'(%)', entity_id: m.soc, hint:'battery' },
      { key:'voltage', label:'Battery Voltage', unitLabel:'(V)', entity_id: m.voltage, hint:'voltage' },
      { key:'solar', label:'Solar Power', unitLabel:'(W)', entity_id: m.solar, hint:'solar' },
      { key:'load', label:'Load Power', unitLabel:'(W)', entity_id: m.load, hint:'power' },
    ];

    const toNum = (x)=>{ const n=Number.parseFloat(String(x)); return Number.isFinite(n)?n:null; };

    for (const r of rows){
      const d=document.createElement('div');
      const st = r.entity_id && hass && hass.states ? hass.states[r.entity_id] : null;
      let unit = st && st.attributes ? (st.attributes.unit_of_measurement || '') : '';
      let valText = '—';
      let subText = '';
      let subTitle = '';

      if (!r.entity_id) {
        // Unmapped: keep it clean.
        valText = '—';
        subText = 'unmapped';
      } else {
        // Mapped but missing/unavailable: show a soft status, keep entity_id in tooltip + secondary line.
        if (!st) {
          valText = 'Not available';
          subText = r.entity_id;
          subTitle = r.entity_id;
        } else {
          let raw = st.state;
          const n = toNum(raw);
          if (r.key === 'soc' && n !== null) {
            let pct = n;
            if (pct <= 1) pct = pct * 100;
            pct = Math.max(0, Math.min(100, pct));
            valText = `${pct.toFixed(0)} %`;
          } else if ((r.key === 'solar' || r.key === 'load') && n !== null) {
            const u = String(unit||'').toLowerCase();
            const w = (u === 'kw') ? (n * 1000) : n;
            valText = `${w.toFixed(0)} W`;
          } else if (r.key === 'voltage' && n !== null) {
            valText = `${n.toFixed(1)} V`;
          } else {
            valText = `${raw}${unit ? (' '+unit) : ''}`;
          }
          subText = r.entity_id;
          subTitle = r.entity_id;
        }
      }

      const keyLabel = ({soc:'SOC', voltage:'voltage', solar:'solar', load:'load'}[r.key] || r.key);
      const mapNow = (!r.entity_id) ? `<button class="btn" data-mapnow="${r.key}" style="margin-top:10px">Map ${keyLabel}</button>` : '';
      const valueClass = (valText === 'Not available') ? 'muted' : '';
      d.innerHTML = `<div class="muted">${r.label} <span class="muted">${r.unitLabel || ''}</span></div><div style="margin-top:2px" class="${valueClass}" title="${subTitle}"><b>${valText}</b></div><div class="muted" style="margin-top:4px" title="${subTitle}">${subText}</div>${mapNow}`;
      root.appendChild(d);
    }

    // wire map-now shortcuts
    for (const btn of root.querySelectorAll('button[data-mapnow]')){
      btn.onclick = () => {
        const key = btn.getAttribute('data-mapnow');
        mapNowShortcut(key);
      };
    }
  }

  function mapNowShortcut(key){
    // Jump to Setup and help the user find likely entities.
    const hints = { soc:'battery', voltage:'voltage', solar:'solar', load:'power' };
    const ids = { soc:'mapSoc', voltage:'mapVoltage', solar:'mapSolar', load:'mapLoad' };

    // Pre-fill + focus the entity list filter (Cockpit). Even after switching tabs, the value remains.
    const f = qs('#filter');
    if (f){
      f.value = hints[key] || '';
      try{ f.focus(); }catch(e){}
      try{ f.scrollIntoView({behavior:'smooth', block:'center'}); }catch(e){}
    }

    // Switch to Setup tab
    try{ qs('#tabSetup').click(); }catch(e){}

    // Focus the relevant manual input and scroll mapping section
    try{
      const input = document.getElementById(ids[key]);
      if (input){ input.focus(); }
      const sugg = document.getElementById('suggestions');
      if (sugg){ sugg.scrollIntoView({behavior:'smooth', block:'start'}); }
    } catch(e){}
  }
  async function getHass(){
    const parent = window.parent;
    if (!parent) throw new Error('No parent window');
    if (parent.hassConnection && parent.hassConnection.then) {
      const conn = await parent.hassConnection;
      if (conn && conn.conn) return { conn: conn.conn, hass: conn.hass };
    }
    if (parent.hass && parent.hass.connection) return { conn: parent.hass.connection, hass: parent.hass };
    throw new Error('Unable to access Home Assistant frontend connection from iframe');
  }

  function setStatus(ok, text, detail){
    const el = qs('#status');
    el.textContent = text;
    el.className = ok ? 'ok' : 'bad';
    const pill = document.getElementById('connPill');
    if (pill){ pill.textContent = ok ? 'connected' : 'error'; pill.className = 'pill ' + (ok ? 'ok' : 'bad'); }
    qs('#statusDetail').textContent = detail || '';
  }



  function scoreEntity(meta, rules){
    const id=(meta.entity_id||'').toLowerCase();
    const name=(meta.name||'').toLowerCase();
    const unit=(meta.unit||'').toLowerCase();
    let s=0;
    for (const kw of (rules.keywords||[])){
      if (id.includes(kw) || name.includes(kw)) s += 3;
    }
    for (const kw of (rules.weak||[])){
      if (id.includes(kw) || name.includes(kw)) s += 1;
    }
    if (rules.units && rules.units.includes(unit)) s += 2;
    // Penalize obviously irrelevant domains
    if (id.startsWith('automation.') || id.startsWith('update.')) s -= 2;
    return s;
  }

  function topCandidates(hass, rules, limit){
    const out=[];
    const states=(hass && hass.states) ? hass.states : {};
    for (const [entity_id, st] of Object.entries(states)){
      const meta={
        entity_id,
        name: (st.attributes && (st.attributes.friendly_name || st.attributes.device_class || '')) || '',
        unit: (st.attributes && st.attributes.unit_of_measurement) || '',
        state: st.state,
      };
      const score=scoreEntity(meta, rules);
      if (score > 0) out.push({score, ...meta});
    }
    out.sort((a,b)=>b.score-a.score);
    return out.slice(0, limit||3);
  }

  function renderSuggestions(hass){
    const root = qs('#suggestions');
    if (!root) return;
    root.innerHTML='';

    const rules={
      soc: { label:'Battery SOC (%)', keywords:['soc','state_of_charge','battery_soc'], units:['%'], weak:['battery'] },
      voltage: { label:'Battery Voltage (V)', keywords:['voltage','battery_voltage','batt_v'], units:['v'], weak:['battery'] },
      solar: { label:'Solar Input Power (W)', keywords:['solar','pv','photovoltaic','panel'], units:['w'], weak:['input','power'] },
      load: { label:'Total Consumption / Load (W)', keywords:['load','consumption','house_power','ac_load','power'], units:['w'], weak:['total','sum'] },
    };

    const mapping = getMapping();
    const fields = ['soc','voltage','solar','load'];

    for (const key of fields){
      const r = rules[key];
      const cands = topCandidates(hass, r, 3);
      const card = document.createElement('div');
      card.className = 'suggest-card';

      const title = document.createElement('div');
      title.className = 'muted';
      title.textContent = r.label;
      card.appendChild(title);

      const list = document.createElement('div');
      if (cands.length) {
        cands.forEach((c, idx) => {
          const row = document.createElement('label');
          row.className = 'choice';
          const id = `sugg-${key}-${idx}`;
          const input = document.createElement('input');
          input.type = 'radio';
          input.name = `sugg-${key}`;
          input.id = id;
          input.value = c.entity_id;
          if (mapping[key] && mapping[key] === c.entity_id) input.checked = true;
          const main = document.createElement('div');
          main.className = 'choice-main';
          main.textContent = c.entity_id;
          const meta = document.createElement('div');
          meta.className = 'choice-meta';
          meta.textContent = `${c.state}${c.unit ? (' ' + c.unit) : ''} · score ${c.score}`;
          const wrap = document.createElement('div');
          wrap.appendChild(main);
          wrap.appendChild(meta);
          row.appendChild(input);
          row.appendChild(wrap);
          list.appendChild(row);
        });
      } else {
        const empty = document.createElement('div');
        empty.className = 'muted';
        empty.textContent = '(no candidates found)';
        empty.style.marginTop = '6px';
        list.appendChild(empty);
      }

      const manualRow = document.createElement('label');
      manualRow.className = 'choice';
      const manualInput = document.createElement('input');
      manualInput.type = 'radio';
      manualInput.name = `sugg-${key}`;
      manualInput.value = '__manual__';
      if (!cands.find(c => c.entity_id === mapping[key])) {
        manualInput.checked = true;
      }
      const manualText = document.createElement('div');
      manualText.className = 'choice-main';
      manualText.textContent = 'Use manual input below';
      manualRow.appendChild(manualInput);
      manualRow.appendChild(manualText);
      list.appendChild(manualRow);

      list.style.marginTop = '6px';
      card.appendChild(list);

      const actions = document.createElement('div');
      actions.className = 'row';
      actions.style.marginTop = '8px';
      const btn = document.createElement('button');
      btn.className = 'btn primary';
      btn.textContent = 'Confirm';
      btn.onclick = () => confirmFieldMapping(key);
      const status = document.createElement('span');
      status.className = 'muted';
      status.id = `confirm-${key}`;
      actions.appendChild(btn);
      actions.appendChild(status);
      card.appendChild(actions);

      root.appendChild(card);
    }
  }
  async function callService(domain, service, data){
    const { conn } = await getHass();
    return conn.callService(domain, service, data || {});
  }

  let _allIds = [];

  function renderEntities(hass, filter){
    const states = hass && hass.states ? hass.states : {};
    const root = qs('#entities');
    root.innerHTML = '';

    const f = (filter || '').trim().toLowerCase();
    const ids = (f ? _allIds.filter(id => id.toLowerCase().includes(f)) : _allIds);

    for (const id of ids){
      const st = states[id];
      const row = document.createElement('div');
      row.className = 'ent';

      const left = document.createElement('div');
      left.style.minWidth = '280px';
      left.innerHTML = `<div class="ent-id">${id}</div><div class="ent-state">${st.state}</div>`;

      const right = document.createElement('div');
      right.className = 'row';

      const domain = id.split('.')[0];
      if (['switch','light','input_boolean'].includes(domain)){
        const onBtn = document.createElement('button');
        onBtn.className = 'btn';
        onBtn.textContent = 'On';
        onBtn.onclick = async () => { await callService('clawdbot','ha_call_service',{domain, service:'turn_on', entity_id:id, service_data:{}}); };

        const offBtn = document.createElement('button');
        offBtn.className = 'btn';
        offBtn.textContent = 'Off';
        offBtn.onclick = async () => { await callService('clawdbot','ha_call_service',{domain, service:'turn_off', entity_id:id, service_data:{}}); };

        right.appendChild(onBtn);
        right.appendChild(offBtn);
      } else {
        const noop = document.createElement('span');
        noop.className = 'muted';
        noop.textContent = 'no controls';
        right.appendChild(noop);
      }

      row.appendChild(left);
      row.appendChild(right);
      root.appendChild(row);
    }

    setStatus(true, 'connected', `Loaded ${ids.length} entities (filter: ${f || 'none'})`);
  }

  async function refreshEntities(){
    const { hass } = await getHass();
    const states = hass && hass.states ? hass.states : {};
    _allIds = Object.keys(states).sort();
    renderEntities(hass, qs('#filter').value);
  }

  async function init(){
    renderConfigSummary();
    fillMappingInputs();
    renderHouseMemory();
    renderMappedValues(null);
    renderSuggestions(null);

    async function switchTab(which){
      const setupTab = qs('#tabSetup');
      const cockpitTab = qs('#tabCockpit');
      const chatTab = qs('#tabChat');
      const viewSetup = qs('#viewSetup');
      const viewCockpit = qs('#viewCockpit');
      const viewChat = qs('#viewChat');
      if (!setupTab || !cockpitTab || !chatTab || !viewSetup || !viewCockpit || !viewChat) return;

      setupTab.classList.toggle('active', which === 'setup');
      cockpitTab.classList.toggle('active', which === 'cockpit');
      chatTab.classList.toggle('active', which === 'chat');

      // Hard display toggles (production UI must isolate views)
      setHidden(viewSetup, which !== 'setup');
      setHidden(viewCockpit, which !== 'cockpit');
      setHidden(viewChat, which !== 'chat');

      if (which === 'cockpit') {
        try{ const { hass } = await getHass(); await refreshEntities(); renderMappedValues(hass); renderHouseMemory(); renderRecommendations(hass); } catch(e){}
      }
      if (which === 'chat') {
        loadChatFromConfig();
        renderChat({ autoScroll: true });
      }
    }

    const bindTab = (id, which) => {
      const el = qs(id);
      if (!el) return;
      el.onclick = (ev) => {
        try{ ev && ev.preventDefault && ev.preventDefault(); }catch(e){}
        try{ ev && ev.stopPropagation && ev.stopPropagation(); }catch(e){}
        switchTab(which);
      };
    };

    bindTab('#tabSetup','setup');
    bindTab('#tabCockpit','cockpit');
    bindTab('#tabChat','chat');

    // Extra robustness: event delegation so clicks on child nodes still switch.
    try{
      const tabs = qs('.tabs');
      if (tabs) tabs.addEventListener('click', (ev) => {
        const t = ev.target;
        if (!t) return;
        const id = t.id || (t.closest ? (t.closest('button')||{}).id : '');
        if (id === 'tabSetup') switchTab('setup');
        if (id === 'tabCockpit') switchTab('cockpit');
        if (id === 'tabChat') switchTab('chat');
      }, true);
    } catch(e){}

    // Normalize initial state (ensures non-active views are truly hidden).
    switchTab('cockpit');

    qs('#refreshBtn').onclick = refreshEntities;
    qs('#clearFilter').onclick = () => { qs('#filter').value=''; getHass().then(({hass})=>renderEntities(hass,'')); };
    qs('#filter').oninput = async () => { try{ const { hass } = await getHass(); renderEntities(hass, qs('#filter').value); } catch(e){} };

    qs('#btnGatewayTest').onclick = async () => {
      qs('#gwTestResult').textContent = 'running…';
      try{
        await callService('clawdbot','gateway_test',{});
        qs('#gwTestResult').textContent = 'triggered';
      } catch(e){
        qs('#gwTestResult').textContent = 'error: ' + String(e);
      }
    };

    const parseJsonSafe = (txt) => {
      const t = String(txt || '').trim();
      if (!t) return {};
      try{ return JSON.parse(t); }catch(e){ return null; }
    };

    const btnSend = qs('#btnSendEvent');
    if (btnSend) btnSend.onclick = async () => {
      const resultEl = qs('#evtResult');
      if (resultEl) resultEl.textContent = 'Sending…';
      const event_type = (qs('#evtType') ? qs('#evtType').value.trim() : 'clawdbot.test');
      const severity = (qs('#evtSeverity') ? qs('#evtSeverity').value : 'info');
      const source = (qs('#evtSource') ? qs('#evtSource').value.trim() : 'panel');
      const attrsTxt = (qs('#evtAttrs') ? qs('#evtAttrs').value : '');
      const attrs = parseJsonSafe(attrsTxt);
      if (attrs === null) {
        if (resultEl) resultEl.textContent = 'attributes JSON is invalid';
        return;
      }
      try{
        await callService('clawdbot','notify_event',{ event_type, severity, source, attributes: attrs });
        if (resultEl) resultEl.textContent = 'Sent (ok)';
      } catch(e){
        if (resultEl) resultEl.textContent = 'Error: ' + String(e);
      }
    };

    qs('#chatSend').onclick = async () => {
      const msg = qs('#chatInput').value.trim();
      if (!msg) return;
      qs('#chatResult').textContent = 'sending…';
      try{
        await callService('clawdbot','send_chat',{message: msg});
        qs('#chatResult').textContent = 'sent';
        qs('#chatInput').value = '';
      } catch(e){
        qs('#chatResult').textContent = 'error: ' + String(e);
      }
    };

    const composer = qs('#chatComposer');
    const composerSend = qs('#chatComposerSend');
    const setSendEnabled = () => {
      if (!composer || !composerSend) return;
      composerSend.disabled = !String(composer.value||'').trim();
    };
    if (composer) composer.addEventListener('input', setSendEnabled);
    setSendEnabled();

    if (composerSend) composerSend.onclick = async () => {
      const input = composer;
      const text = input.value.trim();
      if (!text) return;
      try{
        await callService('clawdbot','chat_append',{ role:'user', text });
        const now = new Date();
        const ts = now.toISOString();
        chatItems.push({ role: 'user', text, ts });
        input.value = '';
        renderChat({ autoScroll: true });
      } catch(e){
        console.warn('chat_append failed', e);
      }
    };
    qs('#chatComposer').addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        qs('#chatComposerSend').click();
      }
    });

    qs('#tabCockpit').onclick();

    try{ const { hass } = await getHass(); setStatus(true,'connected',''); renderSuggestions(hass); renderMappedValues(hass); renderRecommendations(hass); } catch(e){ setStatus(false,'error', String(e)); }
  }

  init();
})();
</script>
</body>
</html>
"""


class ClawdbotPanelView(HomeAssistantView):
    url = PANEL_PATH
    name = "api:clawdbot:panel"
    # This route serves static HTML only (no secrets). It must be embeddable in HA's iframe panel.
    # HA frontend auth is not a cookie header, so iframe navigation would 401 if requires_auth=True.
    requires_auth = False

    async def get(self, request):
        from aiohttp import web
        from json import dumps

        cfg = request.app["hass"].data.get(DOMAIN, {})
        chat_history = cfg.get("chat_history", []) or []
        if not isinstance(chat_history, list):
            chat_history = []
        session_key = cfg.get("target") or DEFAULT_SESSION_KEY
        session_items = [it for it in chat_history if isinstance(it, dict) and it.get("session_key") == session_key]
        if not session_items:
            session_items = [it for it in chat_history if isinstance(it, dict)]
        chat_history = session_items[-50:]
        chat_has_older = len(session_items) > len(chat_history)
        safe_cfg = {
            "gateway_url": cfg.get("gateway_url") or cfg.get("gateway_origin"),
            "has_token": bool(cfg.get("has_token")),
            "target": cfg.get("target"),
            "mapping": cfg.get("mapping", {}),
            "house_memory": cfg.get("house_memory", {}),
            "chat_history": chat_history,
            "chat_history_has_older": chat_has_older,
            "session_key": session_key,
        }
        html = PANEL_HTML.replace("__CONFIG_JSON__", dumps(safe_cfg))
        return web.Response(
            text=html,
            content_type="text/html",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )


class ClawdbotMappingApiView(HomeAssistantView):
    """Authenticated API for reading/writing core-signal mappings.

    This is for headless verification and future UI; not used by the iframe directly.
    """

    url = "/api/clawdbot/mapping"
    name = "api:clawdbot:mapping"
    requires_auth = True

    async def get(self, request):
        from aiohttp import web

        cfg = request.app["hass"].data.get(DOMAIN, {})
        return web.json_response({"ok": True, "mapping": cfg.get("mapping", {})})

    async def post(self, request):
        from aiohttp import web

        hass = request.app["hass"]
        cfg = hass.data.get(DOMAIN, {})
        store: Store = cfg.get("store")
        if store is None:
            return web.json_response({"ok": False, "error": "store not initialized"}, status=500)

        body = await request.json()
        mapping = body.get("mapping")
        if not isinstance(mapping, dict):
            return web.json_response({"ok": False, "error": "mapping must be an object"}, status=400)

        allowed_keys = {"soc", "voltage", "solar", "load"}
        cleaned = {}
        for k, v in mapping.items():
            if k not in allowed_keys:
                continue
            if v is None or v == "":
                cleaned[k] = None
                continue
            if not isinstance(v, str):
                return web.json_response({"ok": False, "error": f"mapping.{k} must be a string"}, status=400)
            cleaned[k] = v

        await store.async_save(cleaned)
        cfg["mapping"] = cleaned
        return web.json_response({"ok": True, "mapping": cleaned})




class ClawdbotPanelSelfTestApiView(HomeAssistantView):
    """Authenticated API that returns computed panel runtime-like diagnostics.

    This is for headless verification (no browser automation): it reports how many
    suggestion candidates would render, how many confirm buttons exist (fixed=4),
    and whether a recommendations v0 estimate would be shown.
    """

    url = "/api/clawdbot/panel_self_test"
    name = "api:clawdbot:panel_self_test"
    requires_auth = True

    async def get(self, request):
        from aiohttp import web

        hass = request.app["hass"]
        cfg = hass.data.get(DOMAIN, {})
        mapping = cfg.get("mapping", {}) or {}

        # Build a cheap states dict
        states = {s.entity_id: s for s in hass.states.async_all()}

        # Mirror the JS heuristic keyword rules
        rules = {
            "soc": {"keywords": ["soc", "state_of_charge", "battery_soc"], "units": ["%"], "weak": ["battery"]},
            "voltage": {"keywords": ["voltage", "battery_voltage", "batt_v"], "units": ["v"], "weak": ["battery"]},
            "solar": {"keywords": ["solar", "pv", "photovoltaic", "panel"], "units": ["w"], "weak": ["input", "power"]},
            "load": {"keywords": ["load", "consumption", "house_power", "ac_load", "power"], "units": ["w"], "weak": ["total", "sum"]},
        }

        def score(ent_id: str, st, rule) -> int:
            name = ""
            unit = ""
            try:
                name = str(st.attributes.get("friendly_name") or st.attributes.get("device_class") or "")
                unit = str(st.attributes.get("unit_of_measurement") or "")
            except Exception:
                pass
            hay = (ent_id + " " + name).lower()
            u = unit.lower()
            s = 0
            for kw in rule.get("keywords", []):
                if kw in hay:
                    s += 3
            for kw in rule.get("weak", []):
                if kw in hay:
                    s += 1
            if rule.get("units") and u in rule["units"]:
                s += 2
            if ent_id.startswith(("automation.", "update.")):
                s -= 2
            return s

        suggestion_counts = {}
        for k, rule in rules.items():
            scored = []
            for ent_id, st in states.items():
                s = score(ent_id, st, rule)
                if s > 0:
                    scored.append((s, ent_id))
            scored.sort(reverse=True)
            suggestion_counts[k] = len(scored[:3])

        # Recommendations v0 visible if soc+load mapped and both numeric
        def to_float(val):
            try:
                return float(str(val))
            except Exception:
                return None

        rec_visible = False
        rec_reason = ""
        if mapping.get("soc") and mapping.get("load"):
            soc_st = states.get(mapping.get("soc"))
            load_st = states.get(mapping.get("load"))
            soc = to_float(soc_st.state) if soc_st else None
            load = to_float(load_st.state) if load_st else None
            if soc is not None and load is not None:
                rec_visible = True
                rec_reason = "soc+load numeric"
            else:
                # In the UI we still render an informational recommendation item.
                rec_visible = True
                rec_reason = "soc/load not numeric or not found"
        else:
            rec_reason = "soc/load not mapped"

        out = {
            "ok": True,
            "panel": {
                "confirm_buttons": 4,
                "suggestion_counts_top3": suggestion_counts,
                "recommendations_v0_visible": rec_visible,
                "recommendations_v0_reason": rec_reason,
            },
        }
        return web.json_response(out)
class ClawdbotHouseMemoryApiView(HomeAssistantView):
    """Authenticated API for reading the derived 'house memory' summary."""

    url = "/api/clawdbot/house_memory"
    name = "api:clawdbot:house_memory"
    requires_auth = True

    async def get(self, request):
        from aiohttp import web

        cfg = request.app["hass"].data.get(DOMAIN, {})
        return web.json_response({"ok": True, "house_memory": cfg.get("house_memory", {})})


class ClawdbotChatHistoryApiView(HomeAssistantView):
    """Authenticated API for reading chat history."""

    url = "/api/clawdbot/chat_history"
    name = "api:clawdbot:chat_history"
    requires_auth = True

    async def get(self, request):
        from aiohttp import web

        hass = request.app["hass"]
        cfg = hass.data.get(DOMAIN, {})
        items = cfg.get("chat_history", []) or []
        if not isinstance(items, list):
            items = []
        items = [it for it in items if isinstance(it, dict)]

        limit = 50
        try:
            limit = int(request.query.get("limit", 50))
        except Exception:
            limit = 50
        if limit < 1:
            limit = 1
        if limit > 500:
            limit = 500

        session_key = request.query.get("session_key")
        if session_key:
            filtered = [it for it in items if it.get("session_key") == session_key]
        else:
            filtered = items
        before_id = request.query.get("before_id")

        if before_id:
            idx = None
            for i, it in enumerate(filtered):
                if it.get("id") == before_id:
                    idx = i
                    break
            if idx is None:
                candidates = filtered
            else:
                candidates = filtered[:idx]
        else:
            candidates = filtered

        if len(candidates) > limit:
            page = candidates[-limit:]
        else:
            page = candidates

        if before_id and len(candidates) > limit:
            has_older = len(candidates) > len(page)
        elif before_id and len(candidates) <= limit:
            has_older = False
        else:
            has_older = len(filtered) > len(page)

        return web.json_response({"ok": True, "items": page, "has_older": has_older})




def _compute_house_memory_from_states(states: dict, mapping: dict | None = None) -> dict:
    """Heuristic summary derived from HA entity ids/names (+ optional user mapping).

    Output format:
      { solar: {present, confidence, evidence:[...]}, ... }

    mapping is the persisted core-signal mapping (soc/voltage/solar/load). If provided,
    we treat mapped entities as strong evidence.
    """

    def _scan(keywords):
        evidence=[]
        for ent_id, st in states.items():
            name=''
            try:
                name=str(st.attributes.get('friendly_name') or '')
            except Exception:
                pass
            hay=(ent_id+' '+name).lower()
            if any(k in hay for k in keywords):
                evidence.append(ent_id)
        return evidence

    # keyword sets (MVP)
    solar_kw=[
        'solar','pv','photovoltaic','panel','mppt','victron','cerbo','smartsolar','renogy','charge_controller'
    ]
    battery_kw=[
        'battery','batt','soc','state_of_charge','shunt','bms','lifepo','voltage','current','amp'
    ]
    grid_kw=[
        'grid','mains','utility','import','export','shore','ac_in','ac input','ac_input'
    ]
    gen_kw=[
        'generator','gen','genset','start','run','running'
    ]

    solar_ev=_scan(solar_kw)
    batt_ev=_scan(battery_kw)
    grid_ev=_scan(grid_kw)
    gen_ev=_scan(gen_kw)

    m = mapping or {}

    def pack(evidence, mapped_ids=None, base_if_mapped=0.75, require_hits: int = 1):
        mapped_ids = [x for x in (mapped_ids or []) if x]
        # Inject mapped ids as strong evidence (dedupe, preserve order)
        seen=set()
        combined=[]
        for ent_id in mapped_ids + list(evidence):
            if ent_id in seen:
                continue
            seen.add(ent_id)
            combined.append(ent_id)

        n=len(combined)
        if n==0:
            return {"present": False, "confidence": 0.0, "evidence": []}

        # For things like grid/generator, avoid "guessing" from a single weak keyword hit.
        if not mapped_ids and n < require_hits:
            return {"present": False, "confidence": 0.0, "evidence": combined[:10]}

        # Confidence:
        # - If user mapped a relevant entity, we assume stronger confidence.
        # - Otherwise ramp based on number of keyword hits.
        conf = min(1.0, 0.25 + 0.12*n)
        if mapped_ids:
            conf = max(conf, base_if_mapped)
        return {"present": True, "confidence": round(conf, 2), "evidence": combined[:10]}

    return {
        # Solar: if the user mapped a solar sensor, treat as strong evidence.
        "solar": pack(solar_ev, mapped_ids=[m.get("solar")], base_if_mapped=0.8, require_hits=1),
        "battery": pack(batt_ev, mapped_ids=[m.get("soc"), m.get("voltage")], base_if_mapped=0.85, require_hits=1),
        # Grid/generator: keep 0 unless we have stronger keyword evidence (>=2 hits).
        "grid": pack(grid_ev, mapped_ids=[], base_if_mapped=0.75, require_hits=2),
        "generator": pack(gen_ev, mapped_ids=[], base_if_mapped=0.75, require_hits=2),
    }
async def async_setup(hass, config):
    conf = config.get(DOMAIN, {})
    # For MVP: always serve panel content from HA itself.
    # This avoids OpenClaw Control UI auth/device-identity and makes the iframe same-origin.
    panel_url = PANEL_PATH

    title = conf.get("title", DEFAULT_TITLE)
    icon = conf.get("icon", DEFAULT_ICON)

    token = conf.get(CONF_TOKEN)
    session_key = conf.get(CONF_SESSION_KEY, DEFAULT_SESSION_KEY)

    # Panel URL is for the browser iframe. Gateway URL is for HA->Clawdbot service calls.
    gateway_origin = str(conf.get(CONF_GATEWAY_URL, _derive_gateway_origin(panel_url))).rstrip("/")
    session = aiohttp.ClientSession()

    # Store sanitized config for the panel (never expose the token).
    hass.data.setdefault(DOMAIN, {})

    # Load persisted mappings
    store = Store(hass, MAPPING_STORE_VERSION, MAPPING_STORE_KEY)

    mapping = await store.async_load() or {}
    if not isinstance(mapping, dict):
        mapping = {}

    # Load / compute house memory summary
    house_store = Store(hass, HOUSEMEM_STORE_VERSION, HOUSEMEM_STORE_KEY)
    house_memory = await house_store.async_load() or {}
    if not isinstance(house_memory, dict):
        house_memory = {}
    # Always compute a fresh snapshot from current states (MVP)
    try:
        states = {s.entity_id: s for s in hass.states.async_all()}
        computed = _compute_house_memory_from_states(states, mapping=mapping)
        house_memory = computed
        await house_store.async_save(house_memory)
    except Exception:
        _LOGGER.exception('Failed to compute house memory')

    hass.data[DOMAIN].update(
        {
            "gateway_origin": gateway_origin,
            "gateway_url": conf.get(CONF_GATEWAY_URL, None),
            "has_token": bool(token),
            "target": session_key,
            "store": store,
            "mapping": mapping,
            "house_store": house_store,
            "house_memory": house_memory,
        }
    )

    # Load chat history
    chat_store = Store(hass, CHAT_STORE_VERSION, CHAT_STORE_KEY)
    chat_history = await chat_store.async_load() or []
    if not isinstance(chat_history, list):
        chat_history = []
    hass.data[DOMAIN].update(
        {
            "chat_store": chat_store,
            "chat_history": chat_history[-500:],
        }
    )

    # HTTP view (served by HA)
    try:
        hass.http.register_view(ClawdbotPanelView)
        hass.http.register_view(ClawdbotMappingApiView)
        hass.http.register_view(ClawdbotPanelSelfTestApiView)
        hass.http.register_view(ClawdbotHouseMemoryApiView)
        hass.http.register_view(ClawdbotChatHistoryApiView)
        _LOGGER.info("Registered Clawdbot panel view → %s", PANEL_PATH)
        _LOGGER.info("Registered Clawdbot mapping API → %s", ClawdbotMappingApiView.url)
    except Exception:
        _LOGGER.exception("Failed to register Clawdbot HTTP views")

    # Panel (iframe)
    try:
        from homeassistant.components.frontend import async_register_built_in_panel

        async_register_built_in_panel(
            hass,
            component_name="iframe",
            sidebar_title=title,
            sidebar_icon=icon,
            frontend_url_path=DOMAIN,
            config={"url": panel_url},
            require_admin=True,
        )
        _LOGGER.info("Registered Clawdbot iframe panel → %s", panel_url)
    except Exception:
        _LOGGER.exception("Failed to register Clawdbot panel")

    # Ensure we close the aiohttp session
    async def _close_session(_evt):
        await session.close()

    hass.bus.async_listen_once("homeassistant_stop", _close_session)

    async def _notify(title: str, message: str) -> None:
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {"title": title, "message": message[:4000]},
            blocking=False,
        )

    # Services
    async def handle_send_chat(call):
        if not token:
            raise RuntimeError("clawdbot.token is required to use services")
        message = call.data.get("message")
        if not message:
            raise RuntimeError("message is required")

        # STRATEGY SHIFT: Use native 'message' tool directly.
        # This bypasses session routing and sends directly to the configured channel.
        # session_key is repurposed as the target channel ID for this mode.
        target_channel = session_key

        payload = {
            "tool": "message",
            "args": {
                "action": "send",
                "channel": "discord",
                "channelId": target_channel, 
                "message": f"[Home Assistant] {message}",
            },
        }
        res = await _gw_post(session, gateway_origin + "/tools/invoke", token, payload)
        await _notify("Clawdbot: send_chat", str(res))

    async def handle_set_mapping(call):
        hass = call.hass
        cfg = hass.data.get(DOMAIN, {})
        store: Store = cfg.get("store")
        if store is None:
            raise RuntimeError("mapping store not initialized")

        mapping = call.data.get("mapping")
        if not isinstance(mapping, dict):
            raise RuntimeError("mapping must be an object")

        allowed_keys = {"soc", "voltage", "solar", "load"}
        cleaned = {}
        for k in allowed_keys:
            v = mapping.get(k, None)
            if v is None or v == "":
                cleaned[k] = None
            elif not isinstance(v, str):
                raise RuntimeError(f"mapping.{k} must be a string")
            else:
                cleaned[k] = v

        await store.async_save(cleaned)
        cfg["mapping"] = cleaned
        await _notify("Clawdbot: set_mapping", __import__("json").dumps(cleaned, indent=2)[:4000])

    async def handle_refresh_house_memory(call):
        hass = call.hass
        cfg = hass.data.get(DOMAIN, {})
        house_store: Store = cfg.get('house_store')
        if house_store is None:
            raise RuntimeError('house memory store not initialized')
        states = {s.entity_id: s for s in hass.states.async_all()}
        computed = _compute_house_memory_from_states(states, mapping=cfg.get('mapping') or {})
        cfg['house_memory'] = computed
        await house_store.async_save(computed)
        await _notify('Clawdbot: house_memory', __import__('json').dumps(computed, indent=2)[:4000])
    async def handle_notify_event(call):
        """Send a structured HA event into OpenClaw (inbound signal).

        Schema:
          event_type: str (must start with 'clawdbot.')
          severity: 'info'|'warning'|'critical'
          source: str
          entity_id: optional str
          attributes: optional dict
        """
        if not token:
            raise RuntimeError("clawdbot.token is required to use services")

        event_type = call.data.get("event_type")
        severity = (call.data.get("severity") or "info").lower()
        source = call.data.get("source")
        entity_id = call.data.get("entity_id")
        attributes = call.data.get("attributes") or {}

        if not isinstance(event_type, str) or not event_type:
            raise RuntimeError("event_type is required")
        if not event_type.startswith("clawdbot."):
            raise RuntimeError("event_type must start with 'clawdbot.'")
        if severity not in {"info", "warning", "critical"}:
            raise RuntimeError("severity must be one of: info, warning, critical")
        if not isinstance(source, str) or not source:
            raise RuntimeError("source is required")
        if entity_id is not None and entity_id != "" and not isinstance(entity_id, str):
            raise RuntimeError("entity_id must be a string")
        if not isinstance(attributes, dict):
            raise RuntimeError("attributes must be an object")

        payload_obj = {
            "event_type": event_type,
            "severity": severity,
            "source": source,
            "entity_id": entity_id or None,
            "attributes": attributes,
        }

        # Log locally (logger + logbook best-effort)
        _LOGGER.info("Clawdbot inbound event: %s", payload_obj)
        try:
            await hass.services.async_call(
                "logbook",
                "log",
                {
                    "name": "Clawdbot",
                    "message": f"{severity.upper()} {event_type} from {source}",
                    "entity_id": entity_id or None,
                },
                blocking=False,
            )
        except Exception:
            # logbook may not be loaded; ignore
            pass

        # Send into OpenClaw session as a message (strict prefix schema prevents abuse)
        # NOTE: Using sessions_send (in-session message) so OpenClaw agent can act on it.
        payload = {
            "tool": "sessions_send",
            "args": {
                "sessionKey": session_key,
                "message": "[Home Assistant event] " + __import__("json").dumps(payload_obj, sort_keys=True),
            },
        }
        res = await _gw_post(session, gateway_origin + "/tools/invoke", token, payload)
        await _notify("Clawdbot: notify_event", str(res))

    async def handle_chat_append(call):
        hass = call.hass
        cfg = hass.data.get(DOMAIN, {})
        store: Store = cfg.get("chat_store")
        if store is None:
            raise RuntimeError("chat history store not initialized")

        role = call.data.get("role")
        text = call.data.get("text")
        session = call.data.get("session_key") or cfg.get("target") or DEFAULT_SESSION_KEY

        if role not in {"user", "agent"}:
            raise RuntimeError("role must be one of: user, agent")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("text is required")
        if not isinstance(session, str) or not session:
            session = DEFAULT_SESSION_KEY

        item = {
            "id": str(time.time_ns()),
            "ts": __import__("datetime").datetime.utcnow().isoformat() + "Z",
            "role": role,
            "session_key": session,
            "text": text,
        }

        items = cfg.get("chat_history", []) or []
        if not isinstance(items, list):
            items = []
        items.append(item)
        if len(items) > 500:
            items = items[-500:]

        await store.async_save(items)
        cfg["chat_history"] = items

    async def handle_chat_fetch(call):
        hass = call.hass
        cfg = hass.data.get(DOMAIN, {})
        store: Store = cfg.get("chat_store")
        if store is None:
            raise RuntimeError("chat history store not initialized")

        limit = 50
        try:
            limit = int(call.data.get("limit", 50))
        except Exception:
            limit = 50
        if limit < 1:
            limit = 1
        if limit > 500:
            limit = 500

        session = call.data.get("session_key") or cfg.get("target") or DEFAULT_SESSION_KEY
        before_id = call.data.get("before_id")

        all_items = await store.async_load() or []
        if not isinstance(all_items, list):
            all_items = []

        filtered = [it for it in all_items if isinstance(it, dict)]
        if session:
            filtered = [it for it in filtered if it.get("session_key") == session]

        if before_id:
            idx = None
            for i, it in enumerate(filtered):
                if it.get("id") == before_id:
                    idx = i
                    break
            if idx is None:
                candidates = filtered
            else:
                candidates = filtered[:idx]
        else:
            candidates = filtered

        if len(candidates) > limit:
            older = candidates[-limit:]
        else:
            older = candidates

        current = cfg.get("chat_history", []) or []
        if not isinstance(current, list):
            current = []

        combined = older + current
        seen = set()
        deduped = []
        for it in combined:
            if not isinstance(it, dict):
                continue
            item_id = it.get("id")
            if item_id and item_id in seen:
                continue
            if item_id:
                seen.add(item_id)
            deduped.append(it)

        if len(deduped) > 500:
            deduped = deduped[-500:]

        await store.async_save(deduped)
        cfg["chat_history"] = deduped

    async def handle_gateway_test(call):
        if not token:
            raise RuntimeError("clawdbot.token is required to use services")
        # Lightweight ping via listing sessions (no side effects)
        payload = {"tool": "sessions_list", "args": {"limit": 1}}
        try:
            res = await _gw_post(session, gateway_origin + "/tools/invoke", token, payload)
            await _notify("Clawdbot: gateway_test", __import__("json").dumps(res, indent=2)[:4000])
        except Exception as e:
            await _notify("Clawdbot: gateway_test", f"ERROR: {e}")
            raise

    async def handle_tools_invoke(call):
        if not token:
            raise RuntimeError("clawdbot.token is required to use services")
        tool = call.data.get("tool")
        args = call.data.get("args", {})
        if not tool:
            raise RuntimeError("tool is required")
        if not isinstance(args, dict):
            raise RuntimeError("args must be an object")

        payload = {"tool": str(tool), "args": args}
        res = await _gw_post(session, gateway_origin + "/tools/invoke", token, payload)
        await _notify(f"Clawdbot: {tool}", str(res))

    async def handle_ha_get_states(call):
        """Return current HA entity states.

        Note: This runs locally inside HA (no Clawdbot gateway calls) because the
        gateway /tools/invoke endpoint does not expose a generic exec tool.
        """
        items = []
        for st in hass.states.async_all():
            items.append({
                "entity_id": st.entity_id,
                "state": st.state,
                "attributes": dict(st.attributes),
                "last_changed": st.last_changed.isoformat() if st.last_changed else None,
                "last_updated": st.last_updated.isoformat() if st.last_updated else None,
            })
        await _notify("Clawdbot: ha_get_states", __import__("json").dumps(items, indent=2))

    async def handle_ha_call_service(call):
        """Call a HA service locally (guardrailed)."""
        domain = call.data.get("domain")
        service_name = call.data.get("service")
        entity_id = call.data.get("entity_id")
        service_data = call.data.get("service_data", {}) or {}
        if not domain or not service_name:
            raise RuntimeError("domain and service are required")
        if service_data and not isinstance(service_data, dict):
            raise RuntimeError("service_data must be an object")

        # Conservative allowlist for outbound actions (expand later).
        allowed: set[tuple[str, str]] = {
            ("light", "turn_on"),
            ("light", "turn_off"),
            ("switch", "turn_on"),
            ("switch", "turn_off"),
            ("input_boolean", "turn_on"),
            ("input_boolean", "turn_off"),
            ("script", "turn_on"),
            ("automation", "trigger"),
            ("persistent_notification", "create"),
            # Safe expansion: temp setpoint is common + bounded; no hvac_mode changes.
            ("climate", "set_temperature"),
            # Optional but generally safe: open/close covers (no position/tilt yet).
            ("cover", "open_cover"),
            ("cover", "close_cover"),
        }
        key = (str(domain), str(service_name))
        if key not in allowed:
            # HomeAssistantError propagates cleanly to websocket service calls (panel/UI will see it).
            raise HomeAssistantError(f"Service not allowed: {domain}.{service_name}")

        target = None
        if entity_id:
            target = {"entity_id": entity_id}

        ctx = getattr(call, "context", None)
        ctx_id = getattr(ctx, "id", None)
        ctx_user = getattr(ctx, "user_id", None)
        _LOGGER.info(
            "Clawdbot outbound HA call: %s.%s target=%s data=%s context_id=%s user_id=%s",
            domain,
            service_name,
            target,
            service_data,
            ctx_id,
            ctx_user,
        )

        await hass.services.async_call(
            str(domain),
            str(service_name),
            service_data,
            target=target,
            blocking=True,
        )
        await _notify("Clawdbot: ha_call_service", f"Called {domain}.{service_name} target={target} data={service_data}")

    hass.services.async_register(DOMAIN, SERVICE_SEND_CHAT, handle_send_chat)
    hass.services.async_register(DOMAIN, SERVICE_NOTIFY_EVENT, handle_notify_event)
    hass.services.async_register(DOMAIN, SERVICE_GATEWAY_TEST, handle_gateway_test)
    hass.services.async_register(DOMAIN, SERVICE_SET_MAPPING, handle_set_mapping)
    hass.services.async_register(DOMAIN, SERVICE_REFRESH_HOUSE_MEMORY, handle_refresh_house_memory)
    hass.services.async_register(DOMAIN, SERVICE_TOOLS_INVOKE, handle_tools_invoke)
    hass.services.async_register(DOMAIN, SERVICE_HA_GET_STATES, handle_ha_get_states)
    hass.services.async_register(DOMAIN, SERVICE_HA_CALL_SERVICE, handle_ha_call_service)
    hass.services.async_register(DOMAIN, "chat_append", handle_chat_append)
    hass.services.async_register(DOMAIN, SERVICE_CHAT_FETCH, handle_chat_fetch)

    _LOGGER.info(
        "Clawdbot services registered (%s.%s, %s.%s, %s.%s, %s.%s)",
        DOMAIN,
        SERVICE_SEND_CHAT,
        DOMAIN,
        SERVICE_TOOLS_INVOKE,
        DOMAIN,
        SERVICE_HA_GET_STATES,
        DOMAIN,
        SERVICE_HA_CALL_SERVICE,
    )

    return True
