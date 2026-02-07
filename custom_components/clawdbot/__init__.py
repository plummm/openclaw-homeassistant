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

import datetime as dt
import hashlib
import logging
import time
from typing import Any

import aiohttp

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import SupportsResponse
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
SERVICE_CHAT_POLL = "chat_poll"
SERVICE_CREATE_DUMMY_ENTITIES = "create_dummy_entities"
SERVICE_CLEAR_DUMMY_ENTITIES = "clear_dummy_entities"
SERVICE_CHAT_SEND = "chat_send"
SERVICE_CHAT_HISTORY_DELTA = "chat_history_delta"
SERVICE_SESSIONS_LIST = "sessions_list"
SERVICE_SESSIONS_SPAWN = "sessions_spawn"
SERVICE_SESSION_STATUS_GET = "session_status_get"


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


def _iso_from_ms(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")

def _runtime(hass) -> dict[str, Any]:
    """Return the runtime config dict (single source of truth for services)."""
    try:
        return (hass.data.get(DOMAIN, {}) or {}).get("runtime", {}) or {}
    except Exception:
        return {}


def _runtime_gateway_parts(hass) -> tuple[aiohttp.ClientSession, str, str, str]:
    """Return (session, gateway_origin, token, session_key) or raise HomeAssistantError."""
    rt = _runtime(hass)
    session: aiohttp.ClientSession | None = rt.get("session")
    gateway_origin = rt.get("gateway_origin")
    token = rt.get("token")
    session_key = rt.get("session_key") or DEFAULT_SESSION_KEY

    if not gateway_origin:
        raise HomeAssistantError("gateway_url not set (use Setup → Save/Apply)")
    if not token:
        raise HomeAssistantError("token not set (use Setup → Save/Apply)")
    if session is None:
        raise HomeAssistantError("gateway session not initialized")
    return session, str(gateway_origin), str(token), str(session_key)


def _runtime_gateway_parts_http(hass) -> tuple[aiohttp.ClientSession | None, str | None, str | None, str | None, str | None]:
    """HTTP-view helper: returns (session, origin, token, session_key, error)."""
    try:
        session, origin, token, session_key = _runtime_gateway_parts(hass)
        return session, origin, token, session_key, None
    except Exception as e:
        return None, None, None, None, str(e)


MAPPING_STORE_KEY = "clawdbot_mapping"
MAPPING_STORE_VERSION = 1

DERIVED_STORE_KEY = "clawdbot_derived"
DERIVED_STORE_VERSION = 1

AGENT0_HIST_STORE_KEY = "clawdbot_agent0_history"
AGENT0_HIST_STORE_VERSION = 1

HOUSEMEM_STORE_KEY = "clawdbot_house_memory"
HOUSEMEM_STORE_VERSION = 1
CHAT_STORE_KEY = "clawdbot_chat_history"
CHAT_STORE_VERSION = 1

CHAT_SESSIONS_STORE_KEY = "clawdbot_chat_sessions"
CHAT_SESSIONS_STORE_VERSION = 1

THEME_STORE_KEY = "clawdbot_theme"
THEME_STORE_VERSION = 1

SETUP_OPTIONS_STORE_KEY = "clawdbot_setup_options"
SETUP_OPTIONS_STORE_VERSION = 1

JOURNAL_STORE_KEY = "clawdbot_journal"
JOURNAL_STORE_VERSION = 1

AGENT_PROFILE_STORE_KEY = "clawdbot_agent_profile"
AGENT_PROFILE_STORE_VERSION = 1

AVATAR_STORE_KEY = "clawdbot_avatar"
AVATAR_STORE_VERSION = 1

AGENT_STATE_WEBHOOK_STORE_KEY = "clawdbot_agent_state_webhook"
AGENT_STATE_WEBHOOK_STORE_VERSION = 1

AVATAR_WEBHOOK_STORE_KEY = "clawdbot_avatar_webhook"
AVATAR_WEBHOOK_STORE_VERSION = 1

OVERRIDES_STORE_KEY = "clawdbot_connection_overrides"
OVERRIDES_STORE_VERSION = 1


PANEL_BUILD_ID = "89337ab.134"
INTEGRATION_BUILD_ID = "158ee3a"

PANEL_JS = r"""
// Clawdbot panel JS (served by HA; avoids inline-script CSP issues)
(function(){
  try{
    const el = document.getElementById('clawdbot-config');
    const txt = el ? (el.textContent || el.innerText || '{}') : '{}';
    window.__CLAWDBOT_CONFIG__ = JSON.parse(txt || '{}');
  } catch(e){ window.__CLAWDBOT_CONFIG__ = {}; }

    // Theme binding: copy HA CSS variables from parent document into this iframe.
    // CSS custom properties do not inherit across iframe boundaries.
    (function syncThemeVars(){
      try{
        const p = window.parent && window.parent.document;
        if (!p) return;
        const src = window.parent.getComputedStyle(p.documentElement);
        const dstEl = document.documentElement;
        const keys = [
          '--primary-background-color','--secondary-background-color','--card-background-color','--ha-card-background',
          '--primary-text-color','--secondary-text-color','--divider-color','--primary-color','--mdc-theme-primary',
          '--ha-card-border-radius','--ha-card-box-shadow','--success-color','--error-color'
        ];
        for (const k of keys){
          const v = src.getPropertyValue(k);
          if (v && v.trim()) dstEl.style.setProperty(k, v.trim());
        }
      } catch(e) {}
    })();

(function(){
  function qs(sel){ return document.querySelector(sel); }
  function setHidden(el, hidden){
    if (!el) return;
    // Use explicit display toggling to avoid any class/CSS interference.
    el.classList.toggle('hidden', !!hidden);
    el.style.display = hidden ? 'none' : '';
  }

  // Chat constants (single source of truth)
  const CHAT_POLL_INTERVAL_MS = 5000;
  const CHAT_POLL_FAST_MS = 2000;
  const CHAT_POLL_INITIAL_MS = 1000;
  const CHAT_POLL_BOOST_WINDOW_MS = 30000;
  const CHAT_DELTA_LIMIT = 200;
  const CHAT_HISTORY_PAGE_LIMIT = 50;
  const CHAT_UI_MAX_ITEMS = 200;

  let chatItems = [];
  let chatHasOlder = false;
  let chatLoadingOlder = false;
  let chatSessionKey = null;
  let chatPollingActive = false;
  let chatPollTimer = null;
  let chatLastSeenIds = new Set();
  let chatPollBoostUntil = 0;
  let chatLastPollTs = null;
  let chatLastPollAppended = 0;
  let chatLastPollError = null;
  const BUILD_ID = ((window.__CLAWDBOT_CONFIG__||{}).build_id || 'unknown');
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


  let __dbgStep = 'boot';
  function dbgStep(step, extra){
    __dbgStep = step;
    if (!DEBUG_UI) return;
    try{ console.debug('[clawdbot] step', step, extra||''); }catch(e){}
    try{
      const el = qs('#debugStamp');
      if (!el) return;
      el.style.display = 'block';
      el.textContent = `build:${BUILD_ID} step:${step}` + (extra ? ` (${extra})` : '');
    } catch(e){}
  }
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

  function updateLoadOlderTop(){
    const wrap = qs('#chatLoadTop');
    const btn = qs('#chatLoadOlderBtn');
    if (!wrap || !btn) return;
    const show = !!(chatHasOlder || chatLoadingOlder);
    wrap.style.display = show ? 'flex' : 'none';
    btn.textContent = chatLoadingOlder ? 'Loading…' : 'Load older';
    btn.disabled = !!chatLoadingOlder;
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
          html += seg.replaceAll('
', '<br/>');
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
    updateLoadOlderTop();

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
      ['session_key', cfg.session_key || '(missing)'],
    ];
    root.innerHTML = '';
    for (const [k,v] of items){
      const d = document.createElement('div');
      d.innerHTML = `<div class="muted">${k}</div><div><b>${String(v)}</b></div>`;
      root.appendChild(d);
    }
  }
  function fillConnectionInputs(){
    const cfg = (window.__CLAWDBOT_CONFIG__ || {});
    const set = (id, val) => { const el = qs('#'+id); if (el) el.value = (val == null ? '' : String(val)); };
    set('connGatewayUrl', cfg.gateway_url || '');
    set('connSessionKey', cfg.session_key || '');
    // Token is never echoed back; leave blank.
  }

  async function saveConnectionOverrides(kind){
    const resultEl = qs('#connResult');
    if (resultEl) resultEl.textContent = (kind === 'reset') ? 'resetting…' : 'saving…';
    try{
      let resp;
      if (kind === 'reset') {
        resp = await callServiceResponse('clawdbot','reset_connection_overrides', {});
      } else {
        const gateway_url = (qs('#connGatewayUrl') ? qs('#connGatewayUrl').value : '').trim();
        const session_key = (qs('#connSessionKey') ? qs('#connSessionKey').value : '').trim();
        const token = (qs('#connToken') ? qs('#connToken').value : '').trim();
        resp = await callServiceResponse('clawdbot','set_connection_overrides', { gateway_url, session_key, token });
      }
      const data = (resp && resp.response) ? resp.response : resp;
      const r = data && data.result ? data.result : data;
      if (r && r.gateway_url !== undefined) window.__CLAWDBOT_CONFIG__.gateway_url = r.gateway_url;
      if (r && r.session_key) window.__CLAWDBOT_CONFIG__.session_key = r.session_key;
      if (r && r.has_token !== undefined) window.__CLAWDBOT_CONFIG__.has_token = !!r.has_token;
      fillConnectionInputs();
      renderConfigSummary();
      if (resultEl) resultEl.textContent = 'ok';
    } catch(e){
      if (resultEl) resultEl.textContent = 'error: ' + String(e);
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
    chatSessionKey = cfg.session_key || null;

    // Dev/test toggle: enable `?debug=1` to force showing paging control for UI QA.
    if (DEBUG_UI && (chatItems && chatItems.length >= 1)) chatHasOlder = true;

    syncChatSeenIds();
  }

  function simpleHash(str){
    // Non-crypto, deterministic 32-bit hash for UI dedupe keys
    let h = 5381;
    const s = String(str || '');
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h) + s.charCodeAt(i);
    return (h >>> 0).toString(16);
  }

  function chatItemKey(it){
    if (!it) return '';
    const id = it.id || it.message_id || it.messageId;
    if (id) return String(id);
    // Fallback: stable-ish key when backend doesn't provide ids
    const role = it.role || '';
    const ts = it.ts || '';
    const text = it.text || '';
    return 'h_' + simpleHash(`${role}|${ts}|${text}`);
  }

  function syncChatSeenIds(){
    // Important: poll loop is the sole owner of advancing seen-set (for stable +N).
    // Only initialize once (first load).
    if (chatLastSeenIds && chatLastSeenIds.size > 0) return;
    const ids = (chatItems || []).map((it)=>chatItemKey(it)).filter(Boolean);
    chatLastSeenIds = new Set(ids);
  }

  async function callServiceResponse(domain, service, data){
    const { conn } = await getHass();
    const payload = data || {};
    if (!conn || typeof conn.sendMessagePromise !== 'function') {
      throw new Error('No HA websocket connection available for service response');
    }
    return conn.sendMessagePromise({
      type: 'call_service',
      domain,
      service,
      service_data: payload,
      return_response: true,
    });
  }



  function setTyping(on){
    const el = qs('#chatTyping');
    if (!el) return;
    if (on) {
      el.textContent = 'Clawdbot is typing…';
      el.style.opacity = '1';
    } else {
      // Keep reserved space to avoid layout jump.
      el.textContent = '';
      el.style.opacity = '0.75';
    }
  }

  function setTokenUsage(text){
    const el = qs('#chatTokenUsage');
    if (el) el.textContent = (text == null ? '—' : String(text));
  }

  async function refreshTokenUsage(){
    try{
      if (!chatSessionKey) { setTokenUsage('—'); return; }
      const resp = await callServiceResponse('clawdbot','session_status_get', { session_key: chatSessionKey });
      const data = (resp && resp.response) ? resp.response : resp;
      const r = data && data.result ? data.result : data;
      const usage = (r && (r.usage || r.Usage || r.data && r.data.usage)) || null;
      const total = usage && (usage.totalTokens || usage.total_tokens || usage.tokens || usage.total) ;
      if (total != null) setTokenUsage(total);
      else setTokenUsage('—');
    } catch(e){
      setTokenUsage('—');
    }
  }

  function ensureSessionSelectValue(){
    const sel = qs('#chatSessionSelect');
    if (!sel) return;
    const current = chatSessionKey || sel.value || '';
    const fallback = current || (window.__CLAWDBOT_CONFIG__ && (window.__CLAWDBOT_CONFIG__.session_key)) || 'main';
    if (!sel.options || sel.options.length === 0) {
      const o = document.createElement('option');
      o.value = fallback;
      o.textContent = fallback;
      sel.appendChild(o);
      sel.value = fallback;
    }
  }

  async function refreshSessions(){
    const sel = qs('#chatSessionSelect');
    if (!sel) return;
    // Always show *something* immediately so the control isn't an empty chevron.
    ensureSessionSelectValue();
    try{
      const apiPath = 'clawdbot/sessions?limit=50';
      // Use parent callApi when available, otherwise fall back to authenticated fetch
      const resp = await callServiceResponse('clawdbot','sessions_list', { limit: 50 });
      const data = (resp && resp.response) ? resp.response : resp;

      const r = data && data.result ? data.result : data;
      const sessions = (r && (r.sessions || r.items || r.result || r)) || [];
      const arr = Array.isArray(sessions) ? sessions : (sessions.sessions || sessions.items || []);
      // Preserve existing selection
      const current = chatSessionKey || sel.value || '';
      sel.innerHTML = '';
      const mkOpt = (value, label) => {
        const o = document.createElement('option');
        o.value = value;
        o.textContent = label;
        return o;
      };
      const seen = new Set();
      // Ensure there's always a visible value even if sessions_list parse fails.
      const fallback = current || (window.__CLAWDBOT_CONFIG__ && (window.__CLAWDBOT_CONFIG__.session_key)) || 'main';
      if (fallback) { sel.appendChild(mkOpt(fallback, fallback)); seen.add(fallback); }
      for (const s of arr){
        const key = s && (s.sessionKey || s.session_key || s.key || s.id);
        if (!key || seen.has(key)) continue;
        const label = s.label || s.name || '';
        sel.appendChild(mkOpt(key, label ? (label + ' — ' + key) : key));
        seen.add(key);
      }
      sel.value = current || fallback;
    } catch(e){
      // best-effort only
      if (DEBUG_UI) console.debug('[clawdbot chat] refreshSessions failed', e);
    }
  }

  function maxChatTs(){
    let max = '';
    for (const it of (chatItems || [])){
      const ts = it && it.ts ? String(it.ts) : '';
      if (ts && ts > max) max = ts;
    }
    return max;
  }

  async function loadChatLatest(){
    try{
      const params = new URLSearchParams();
      params.set('limit', '50');
      if (chatSessionKey) params.set('session_key', chatSessionKey);
      const apiPath = 'clawdbot/chat_history?' + params.toString();

      // Use service response to avoid iframe auth/context issues.
      const resp = await callServiceResponse('clawdbot','chat_history_delta', { session_key: chatSessionKey, limit: CHAT_HISTORY_PAGE_LIMIT });
      const data = (resp && resp.response) ? resp.response : resp;
      chatItems = (data && Array.isArray(data.items)) ? data.items : [];
      chatHasOlder = !!(data && data.has_older);
      syncChatSeenIds();
    } catch(e){
      if (DEBUG_UI) console.debug('[clawdbot chat] loadChatLatest failed', e);
    }
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
      // Older paging via service response (no /api auth boundary)
      const resp = await callServiceResponse('clawdbot','chat_history_delta', { session_key: chatSessionKey, before_id: beforeId, limit: CHAT_HISTORY_PAGE_LIMIT });
      const data = (resp && resp.response) ? resp.response : resp;
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
      syncChatSeenIds();
    } catch(e){
      console.warn('chat_history fetch failed', e);
    } finally {
      chatLoadingOlder = false;
      renderChat({ preserveScroll: true });
    }
  }

  function stopChatPolling(){
    chatPollingActive = false;
    if (chatPollTimer) {
      clearTimeout(chatPollTimer);
      chatPollTimer = null;
    }
    if (DEBUG_UI) {
      chatLastPollDebugDetail = 'stopped';
    }
    updateChatPollDebug();
    if (DEBUG_UI) console.debug('[clawdbot chat] polling stopped');
  }

  let chatLastPollDebugDetail = '';

  function updateChatPollDebug(){
    const el = qs('#chatPollDebug');
    if (!el) return;
    if (!DEBUG_UI) { el.style.display = 'none'; return; }
    const last = chatLastPollTs ? new Date(chatLastPollTs).toLocaleTimeString() : '—';
    const err = chatLastPollError ? (' err:' + chatLastPollError) : '';
    const detail = chatLastPollDebugDetail ? (' · ' + chatLastPollDebugDetail) : '';
    el.textContent = `Polling: ${chatPollingActive ? 'on' : 'off'} · last: ${last} · +${chatLastPollAppended || 0}${err}${detail}`;
    el.style.display = 'inline';
  }

  function startChatPolling(){
    if (chatPollingActive) return;
    chatPollingActive = true;
    updateChatPollDebug();
    scheduleChatPoll(CHAT_POLL_INITIAL_MS);
  }

  function boostChatPolling(){
    chatPollBoostUntil = Date.now() + CHAT_POLL_BOOST_WINDOW_MS;
  }

  function scheduleChatPoll(delayMs){
    if (!chatPollingActive) return;
    if (chatPollTimer) clearTimeout(chatPollTimer);
    chatPollTimer = setTimeout(pollSessionsHistory, Math.max(500, delayMs || 0));
  }

  async function pollSessionsHistory(){
    if (!chatPollingActive) return;
    if (!chatSessionKey) {
      chatLastPollTs = Date.now();
      chatLastPollAppended = 0;
      chatLastPollError = null;
      updateChatPollDebug();
      scheduleChatPoll(CHAT_POLL_INTERVAL_MS);
      return;
    }

    const currentSession = chatSessionKey;
    try{
      const seenBefore = chatLastSeenIds ? new Set(Array.from(chatLastSeenIds)) : new Set();
      await callService('clawdbot','chat_poll',{ session_key: currentSession, limit: CHAT_HISTORY_PAGE_LIMIT });
      chatLastPollTs = Date.now();
      chatLastPollError = null;

      // Incremental refresh: fetch only items newer than current max ts (avoids capped moving-window)
      const afterTs = maxChatTs();
      const resp = await callServiceResponse('clawdbot','chat_history_delta', { session_key: currentSession, after_ts: afterTs || null, limit: CHAT_DELTA_LIMIT });
      const data = (resp && resp.response) ? resp.response : resp;
      const newer = (data && Array.isArray(data.items)) ? data.items : [];

      // Merge new items onto existing list
      if (newer.length) {
        const existing = new Set((chatItems || []).map(chatItemKey));
        for (const it of newer){
          const k = chatItemKey(it);
          if (!k || existing.has(k)) continue;
          chatItems.push(it);
          existing.add(k);
        }
        // keep last 200 for UI responsiveness
        if (chatItems.length > 200) chatItems = chatItems.slice(-200);
      }

      // +N: count newly-seen agent keys among returned items
      let appendedCount = 0;
      const nextSeen = new Set(Array.from(seenBefore));
      for (const it of newer){
        const key = chatItemKey(it);
        if (!key) continue;
        if (nextSeen.has(key)) continue;
        nextSeen.add(key);
        if (it && it.role === 'agent') appendedCount += 1;
      }
      chatLastSeenIds = nextSeen;
      chatLastPollAppended = appendedCount;

      renderChat({ preserveScroll: true });

      if (DEBUG_UI) {
        const tail = (chatItems || []).slice(-3).map((it)=>({
          id: (it && (it.id || it.message_id || it.messageId)) || null,
          key: chatItemKey(it),
          role: it && it.role,
          ts: it && it.ts,
        }));
        chatLastPollDebugDetail = `seen:${seenBefore.size} items:${(chatItems||[]).length} new:${newer.length} tailTs:${(tail[tail.length-1]&&tail[tail.length-1].ts)||'—'}`;
        console.debug('[clawdbot chat] poll ok', {session: currentSession, appended: chatLastPollAppended, afterTs, newerCount: newer.length, tail});
      }
    } catch(e){
      chatLastPollTs = Date.now();
      chatLastPollAppended = 0;
      chatLastPollError = String(e && (e.message || e)).slice(0, 120);
      if (DEBUG_UI) console.debug('[clawdbot chat] poll error', e);
    }

    updateChatPollDebug();

    if (chatLastPollAppended) boostChatPolling();
    const delay = (Date.now() < chatPollBoostUntil) ? CHAT_POLL_FAST_MS : CHAT_POLL_INTERVAL_MS;
    scheduleChatPoll(delay);
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

    // If opened as top-window (not embedded), we cannot access HA frontend connection.
    if (window === window.top) {
      throw new Error('Top-window mode: no parent hass connection');
    }

    // Path 1: legacy global hassConnection promise (add timeout; some builds keep it pending)
    try{
      if (parent.hassConnection && parent.hassConnection.then) {
        const timeoutMs = 1500;
        const hc = await Promise.race([
          parent.hassConnection,
          new Promise((_, rej) => setTimeout(() => rej(new Error('hassConnection timeout')), timeoutMs)),
        ]);
        if (hc && hc.conn) {
          // IMPORTANT: some HA builds resolve hassConnection with {conn} but without {hass}.
          // Do not return early in that case; keep the conn and continue searching for hass.
          let hass = hc.hass || null;
          if (!hass || !hass.states) {
            try{ hass = (parent.hass && parent.hass.states) ? parent.hass : null; } catch(e) {}
          }
          if (hass && hass.states) {
            try{ if (DEBUG_UI) console.debug('[clawdbot] getHass via hassConnection', true); }catch(e){};
            return { conn: hc.conn, hass };
          }
          parent.__clawdbotConn = hc.conn;
        }
      }
    } catch(e) {}

    // Path 2: legacy global hass
    try{
      if (parent.hass && parent.hass.connection) { try{ if (DEBUG_UI) console.debug('[clawdbot] getHass via parent.hass', !!(parent.hass && parent.hass.states)); }catch(e){}; return { conn: parent.hass.connection, hass: parent.hass }; }
    } catch(e) {}

    // If we have a conn from Path1 but still no hass, try to synthesize hass via DOM and return.
    try{
      const fallbackConn = parent.__clawdbotConn || null;
      if (fallbackConn) {
        const doc = parent.document;
        const ha = doc && doc.querySelector ? doc.querySelector('home-assistant') : null;
        const main = ha && ha.shadowRoot && ha.shadowRoot.querySelector ? ha.shadowRoot.querySelector('home-assistant-main') : null;
        const hass = (main && (main.hass || main._hass)) || (ha && (ha.hass || ha._hass)) || null;
        if (hass && hass.states) return { conn: fallbackConn, hass };
      }
    } catch(e) {}

    // Path 3: query DOM for HA root element, then read hass / hassConnection
    try{
      const doc = parent.document;
      const roots = [
        doc && doc.querySelector && doc.querySelector('home-assistant'),
        doc && doc.querySelector && doc.querySelector('home-assistant-main'),
        doc && doc.querySelector && doc.querySelector('hc-main'),
      ].filter(Boolean);

      for (const r of roots){
        try{
          if (r.hassConnection && r.hassConnection.then) {
            const timeoutMs = 1500;
            const hc = await Promise.race([
              r.hassConnection,
              new Promise((_, rej) => setTimeout(() => rej(new Error('hassConnection timeout')), timeoutMs)),
            ]);
            if (hc && hc.conn) { try{ if (DEBUG_UI) console.debug('[clawdbot] getHass via root.hassConnection', !!(hc.hass && hc.hass.states)); }catch(e){}; return { conn: hc.conn, hass: hc.hass }; }
          }
        } catch(e) {}
        try{
          if (r.hass && r.hass.connection) { try{ if (DEBUG_UI) console.debug('[clawdbot] getHass via root.hass', !!(r.hass && r.hass.states)); }catch(e){}; return { conn: r.hass.connection, hass: r.hass }; }
        } catch(e) {}
        // some HA builds tuck hass on appEl._hass
        try{
          if (r._hass && r._hass.connection) { try{ if (DEBUG_UI) console.debug('[clawdbot] getHass via root._hass', !!(r._hass && r._hass.states)); }catch(e){}; return { conn: r._hass.connection, hass: r._hass }; }
        } catch(e) {}
        // shadowRoot hop
        try{
          const sr = r.shadowRoot;
          if (sr){
            const inner = sr.querySelector('home-assistant') || sr.querySelector('home-assistant-main');
            if (inner && inner.hass && inner.hass.connection) return { conn: inner.hass.connection, hass: inner.hass };
          }
        } catch(e) {}
      }
    } catch(e) {}



    // Path 4: explicit HA shadow DOM traversal (home-assistant → shadowRoot → home-assistant-main)
    try{
      const doc = parent.document;
      const ha = doc && doc.querySelector ? doc.querySelector('home-assistant') : null;
      const main = ha && ha.shadowRoot && ha.shadowRoot.querySelector ? ha.shadowRoot.querySelector('home-assistant-main') : null;
      if (main) {
        const hass = main.hass || main._hass || null;
        const conn = hass && hass.connection ? hass.connection : null;
        if (hass && conn) { try{ if (DEBUG_UI) dbgStep('got-hass-shadow');
        console.debug('[clawdbot] getHass via shadowRoot', !!(hass && hass.states), !!(hass && hass.connection)); }catch(e){}; return { conn, hass }; }
      }
    } catch(e) {}
    throw new Error('Unable to access Home Assistant frontend connection from iframe');
  }

  function setStatus(ok, text, detail, hint){
    try{ if (DEBUG_UI) console.debug('[clawdbot] setStatus', {ok, text, detail, hint}); } catch(e) {}
    const el = qs('#status');
    if (!el) return;
    el.textContent = text;
    el.className = ok ? 'ok' : 'bad';
    const pill = document.getElementById('connPill');
    if (pill){ pill.textContent = ok ? 'connected' : 'error'; pill.className = 'pill ' + (ok ? 'ok' : 'bad'); }
    qs('#statusDetail').textContent = detail || '';
    const hintEl = qs('#statusHint');
    if (hintEl) hintEl.textContent = hint || '';
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
    const { conn, hass } = await getHass();
    const payload = data || {};

    // Preferred: hass.callService
    if (hass && typeof hass.callService === 'function') {
      if (DEBUG_UI) console.debug('[clawdbot] callService via hass.callService', domain, service);
      return hass.callService(domain, service, payload);
    }

    // Fallback: websocket message (Home Assistant connection)
    if (conn && typeof conn.sendMessagePromise === 'function') {
      if (DEBUG_UI) console.debug('[clawdbot] callService via conn.sendMessagePromise', domain, service);
      return conn.sendMessagePromise({
        type: 'call_service',
        domain,
        service,
        service_data: payload,
      });
    }

    throw new Error('Unable to call service (no hass.callService or conn.sendMessagePromise)');
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


  
  async function fetchStatesWs(conn){
    if (!conn || typeof conn.sendMessagePromise !== 'function') throw new Error('WS connection unavailable');
    const arr = await conn.sendMessagePromise({ type: 'get_states' });
    const out = {};
    if (Array.isArray(arr)) {
      for (const it of arr) {
        if (it && it.entity_id) out[it.entity_id] = it;
      }
    }
    return out;
  }

async function fetchStatesRest(hass){
    // Fallback when hass.states is empty/unavailable in iframe context.
    const token = (() => { try{ return (hass && hass.auth && hass.auth.data && hass.auth.data.access_token) || (hass && hass.auth && hass.auth.accessToken) || null; } catch(e){ return null; } })();
    const headers = { 'Accept': 'application/json' };
    try{ if (DEBUG_UI) console.debug('[clawdbot] rest auth token?', {hasAuth: !!(hass && hass.auth), hasData: !!(hass && hass.auth && hass.auth.data), hasAccessToken: !!token}); }catch(e){}
    if (token) { headers['Authorization'] = `Bearer ${token}`; try{ if (DEBUG_UI) console.debug('[clawdbot] rest using bearer auth'); }catch(e){} }
    const r = await fetch('/api/states', { credentials: 'include', headers });
    let len = null;
    if (!r.ok) {
      try{ if (DEBUG_UI) console.debug('[clawdbot] /api/states status', r.status, 'www-authenticate', r.headers.get('www-authenticate')); }catch(e){}
      throw new Error('REST /api/states failed: ' + r.status);
    }
    const arr = await r.json();
    if (Array.isArray(arr)) len = arr.length;
    try{ if (DEBUG_UI) console.debug('[clawdbot] /api/states ok len', len); }catch(e){}
    const out = {};
    if (Array.isArray(arr)) {
      for (const it of arr) {
        if (it && it.entity_id) out[it.entity_id] = it;
      }
    }
    return out;
  }

  async function refreshEntities(){
    try{ if (DEBUG_UI) dbgStep('refresh-start');
    console.debug('[clawdbot] refreshEntities start'); }catch(e) {}
    const { hass } = await getHass();
    let states = (hass && hass.states) ? hass.states : {};
    let n = 0;
    try{ n = Object.keys(states||{}).length; }catch(e){}
    if (!n) {
      // Prefer websocket get_states when available (avoids REST 401 in iframe context)
      try{
        const conn = (hass && hass.connection) ? hass.connection : null;
        if (DEBUG_UI) console.debug('[clawdbot] hass.states empty; trying WS get_states');
        states = await fetchStatesWs(conn);
      } catch(e) {
        try{ if (DEBUG_UI) console.debug('[clawdbot] WS get_states failed; falling back to REST', e); }catch(_e){}
        try{
          states = await fetchStatesRest(hass);
        } catch(e2){
          setStatus(false,'error', String(e2));
          throw e2;
        }
      }
    }
    _allIds = Object.keys(states).sort();
    buildMappingDatalist(hass);
    renderEntities(hass, qs('#filter').value);

  function buildMappingDatalist(hass){
    const dl = document.getElementById('entityIdList');
    if (!dl) return;
    const states = hass && hass.states ? hass.states : {};
    dl.innerHTML = '';
    // Filter out noisy domains for mapping UX; keep sensors, numbers by default.
    const allow = (id) => {
      if (!id || typeof id !== 'string') return false;
      if (id.startsWith('automation.') || id.startsWith('update.')) return false;
      return true;
    };
    for (const id of _allIds){
      if (!allow(id)) continue;
      const st = states[id];
      const name = (st && st.attributes && st.attributes.friendly_name) ? String(st.attributes.friendly_name) : '';
      const opt = document.createElement('option');
      opt.value = id;
      if (name) opt.label = name;
      dl.appendChild(opt);
    }
  }
  }

  async function init(){
    try{ setStatus(false, 'checking…', 'initializing…', (window===window.top)?'Tip: open via the Home Assistant sidebar panel (iframe) to access hass connection.':''); } catch(e) {}
    try{ if (DEBUG_UI) dbgStep('init-start');
    console.debug('[clawdbot] init start', {top: window===window.top}); } catch(e) {}
    try {
    renderConfigSummary();
    fillConnectionInputs();
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
    try{ if (DEBUG_UI) dbgStep('before-getHass');
    console.debug('[clawdbot] before getHass'); } catch(e) {}
        try{ const { hass } = await getHass(); await refreshEntities(); renderMappedValues(hass); renderHouseMemory(); renderRecommendations(hass); } catch(e){}
      }
      if (which === 'chat') {
        loadChatFromConfig();
        ensureSessionSelectValue();
        await refreshSessions();
        // Prefer live fetch for the selected session (keeps dropdown + history in sync)
        await loadChatLatest();
        renderChat({ autoScroll: true });
        await refreshTokenUsage();
        startChatPolling();
        updateChatPollDebug();
      } else {
        stopChatPolling();
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

    const btnSave = qs('#btnConnSave');
    if (btnSave) btnSave.onclick = () => saveConnectionOverrides('save');
    const btnReset = qs('#btnConnReset');
    if (btnReset) btnReset.onclick = () => saveConnectionOverrides('reset');

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

    const composer = qs('#chatComposer');
    const composerSend = qs('#chatComposerSend');
    const loadOlderBtn = qs('#chatLoadOlderBtn');
    if (loadOlderBtn) loadOlderBtn.onclick = () => { loadOlderChat(); };

    const sessionSel = qs('#chatSessionSelect');
    if (sessionSel) sessionSel.onchange = async () => {
      chatSessionKey = sessionSel.value || null;
      await loadChatLatest();
      renderChat({ autoScroll: true });
      await refreshTokenUsage();
      if (chatPollingActive) scheduleChatPoll(CHAT_POLL_INITIAL_MS);
    };

    const newSessionBtn = qs('#chatNewSessionBtn');
    if (newSessionBtn) newSessionBtn.onclick = async () => {
      const label = prompt('New session label (optional):', '');
      try{
        const resp = await callServiceResponse('clawdbot','sessions_spawn', { label: label || undefined });
        const data = (resp && resp.response) ? resp.response : resp;
        const r = data && data.result ? data.result : data;
        const key = r && (r.sessionKey || r.session_key || r.key);
        await refreshSessions();
        if (key && sessionSel) {
          sessionSel.value = key;
          chatSessionKey = key;
          await loadChatLatest();
          renderChat({ autoScroll: true });
          await refreshTokenUsage();
          if (chatPollingActive) scheduleChatPoll(CHAT_POLL_INITIAL_MS);
        }
      } catch(e){
        console.warn('sessions_spawn failed', e);
      }
    };
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

      // optimistic append to local history store
      try{ await callService('clawdbot','chat_append',{ role:'user', text, session_key: chatSessionKey }); } catch(e){}
      const now = new Date();
      const ts = now.toISOString();
      chatItems.push({ role: 'user', text, ts, session_key: chatSessionKey });
      input.value = '';
      renderChat({ autoScroll: true });
      boostChatPolling();
      if (chatPollingActive) scheduleChatPoll(CHAT_POLL_INITIAL_MS);

      // deterministic in-flight indicator while the gateway call is pending
      setTyping(true);
      try{
        await callService('clawdbot','chat_send',{ session_key: chatSessionKey, message: text });
      } catch(e){
        console.warn('sessions_send failed', e);
      } finally {
        setTyping(false);
        await refreshTokenUsage();
      }
    };
    qs('#chatComposer').addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        qs('#chatComposerSend').click();
      }
    });

    qs('#tabCockpit').onclick();

    try{ const { hass } = await getHass(); dbgStep('connected');
    setStatus(true,'connected',''); renderSuggestions(hass); renderMappedValues(hass); renderRecommendations(hass); } catch(e){ const hint = (window === window.top) ? 'Tip: open via the Home Assistant sidebar panel (iframe) to access hass connection.' : ''; setStatus(false,'error', String(e), hint); }
    } catch(e) {
      try{ if (DEBUG_UI) dbgStep('init-fatal');
      console.error('[clawdbot] init fatal', e); } catch(_e) {}
      const hint = (window === window.top) ? 'Tip: open via the Home Assistant sidebar panel (iframe) to access hass connection.' : '';
      try{ setStatus(false,'error', String(e), hint); } catch(_e) {}
    }
  }

  function __clawdbotBoot(){
    if (window.__clawdbotPanelInit) return;
    window.__clawdbotPanelInit = true;
    try{ init(); } catch(e){
      try{ if (typeof DEBUG_UI !== 'undefined' && DEBUG_UI) console.error('[clawdbot] init threw', e); }catch(_e){}
      // retry once on next tick in case DOM wasn't ready
      try{ setTimeout(() => { try{ init(); } catch(_e2){} }, 50); } catch(_e) {}
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', __clawdbotBoot, { once: true });
  } else {
    __clawdbotBoot();
  }
})();
})();

"""

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
    html{
      --cb-page-bg:color-mix(in srgb, var(--primary-background-color) 92%, #000 8%);
      --cb-card-bg:color-mix(in srgb, var(--ha-card-background, var(--card-background-color)) 92%, #fff 8%);
      --cb-surface-bg:color-mix(in srgb, var(--ha-card-background, var(--card-background-color)) 90%, var(--primary-background-color) 10%);
      --cb-border:color-mix(in srgb, var(--divider-color) 68%, var(--primary-text-color) 22%);
      --cb-border-strong:color-mix(in srgb, var(--divider-color) 58%, var(--primary-text-color) 32%);
      --cb-shadow:0 10px 26px rgba(0,0,0,.14);
      --cb-shadow-soft:0 6px 16px rgba(0,0,0,.12);
      --cb-control-bg:color-mix(in srgb, var(--ha-card-background, var(--card-background-color)) 86%, var(--primary-background-color) 14%);

      /* Theme variables (overridden by JS presets) */
      --claw-accent-a: rgba(0,245,255,.90);
      --claw-accent-b: rgba(123,44,255,.90);
      --claw-accent-c: rgba(255,62,142,.70);
      --claw-bg-0: color-mix(in srgb, var(--cb-page-bg) 65%, transparent);
      --claw-bg-1: rgba(0,245,255,.14);
      --claw-bg-2: rgba(123,44,255,.14);
      --claw-bg-3: rgba(255,62,142,.10);
      --claw-btn-glow: rgba(0,245,255,.34);
      /* Contrast tint for main surface/cards (intentionally different from page background) */
      --claw-surface-tint: color-mix(in srgb, var(--claw-accent-c) 22%, transparent);
    }
    html{background:var(--cb-page-bg);}
    body{font-family:var(--primary-font-family, var(--ha-font-family, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial));
      padding:18px;max-width:none;margin:0;
      letter-spacing:-0.01em; line-height:1.45;
      /* Themed background (full-bleed, non-solid) */
      background:
        radial-gradient(1400px 620px at 18% 0%, var(--claw-bg-1), transparent 60%),
        radial-gradient(1200px 620px at 82% 12%, var(--claw-bg-2), transparent 62%),
        radial-gradient(1000px 620px at 70% 92%, var(--claw-bg-3), transparent 58%),
        radial-gradient(900px 520px at 50% 40%, color-mix(in srgb, var(--claw-accent-c) 24%, transparent), transparent 70%),
        linear-gradient(180deg,
          color-mix(in srgb, var(--secondary-background-color) 65%, var(--cb-page-bg)) 0%,
          var(--cb-page-bg) 340px);
      color:var(--primary-text-color);
    }
    .surface{max-width:980px;margin:0 auto;}
    input,button,textarea,select{font:inherit;}
    input,textarea,select,button{
      height:44px;
      padding:0 14px;
      border-radius:12px;
      border:1px solid color-mix(in srgb, var(--cb-border) 70%, var(--claw-accent-a) 14%);
      background:linear-gradient(135deg,
        color-mix(in srgb, var(--cb-control-bg) 90%, transparent),
        color-mix(in srgb, var(--claw-bg-1) 18%, transparent));
      color:var(--primary-text-color);
      outline:none;
      box-shadow:inset 0 0 0 1px color-mix(in srgb, var(--divider-color) 55%, transparent);
    }
    input,textarea,select{width:100%;}
    textarea{height:auto;padding:12px 14px;}
    input:focus,textarea:focus,select:focus,button:focus-visible{
      border-color:color-mix(in srgb, var(--claw-accent-a) 55%, var(--cb-border));
      outline:2px solid color-mix(in srgb, var(--claw-accent-a) 40%, transparent);
      outline-offset:2px;
      box-shadow:0 0 0 3px color-mix(in srgb, var(--claw-accent-a) 22%, transparent);
    }
    code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:12px;}
    h1{font-size:24px;line-height:1.2;font-weight:800;margin:0 0 8px 0;letter-spacing:-0.2px;}
    h2{font-size:17px;line-height:1.25;font-weight:800;margin:0 0 10px 0;}
    .surface{
      /* Outer container: use a contrasting tint so the app body is colorful too */
      background:
        radial-gradient(900px 420px at 12% 18%, color-mix(in srgb, var(--claw-surface-tint) 85%, transparent), transparent 62%),
        radial-gradient(700px 420px at 88% 22%, color-mix(in srgb, var(--claw-accent-a) 12%, transparent), transparent 60%),
        linear-gradient(180deg,
          color-mix(in srgb, var(--cb-surface-bg) 84%, transparent),
          color-mix(in srgb, var(--claw-bg-1) 10%, transparent));
      border-radius:18px;
      padding:20px;
      border:1px solid color-mix(in srgb, var(--cb-border) 70%, var(--claw-accent-c) 18%);
      box-shadow:var(--cb-shadow);
      backdrop-filter: blur(10px);
    }
    .row{display:flex;gap:12px;align-items:center;flex-wrap:wrap;}
    .card{border:1px solid color-mix(in srgb, var(--cb-border-strong) 70%, var(--claw-accent-b) 12%);border-radius:18px;padding:18px;margin:16px 0;
      background:linear-gradient(180deg,
        color-mix(in srgb, var(--cb-card-bg) 92%, transparent),
        color-mix(in srgb, var(--claw-bg-2) 10%, transparent));
      box-shadow:var(--cb-shadow-soft);
      backdrop-filter: blur(10px);
    }
    .muted{color:var(--secondary-text-color);font-size:12.5px;}
    .ok{color:#0a7a2f;}
    .bad{color:#a00000;}

    /* Agent typography upgrades (CSP-safe, uses HA font stack) */
    .agent-title{font-size:30px;font-weight:950;letter-spacing:-0.7px;line-height:1.05;}
    .agent-desc{font-size:15.5px;font-weight:750;letter-spacing:-0.12px;}

    /* Mood label: bigger + modern accent + sentiment color */
    .agent-mood{font-size:13px;font-weight:950;letter-spacing:0.08em;text-transform:uppercase;
      color:color-mix(in srgb, var(--claw-accent-a) 70%, var(--secondary-text-color));
      text-shadow:0 1px 0 rgba(0,0,0,.25), 0 0 16px color-mix(in srgb, var(--claw-accent-a) 28%, transparent);
      margin-left:10px;
    }
    .agent-mood.mood-alert{color:#ff4040; text-shadow:0 1px 0 rgba(0,0,0,.25), 0 0 18px rgba(255,64,64,.35);} 
    .agent-mood.mood-focused{color:#b57bff; text-shadow:0 1px 0 rgba(0,0,0,.25), 0 0 18px rgba(181,123,255,.35);} 
    .agent-mood.mood-degraded{color:#ffa600; text-shadow:0 1px 0 rgba(0,0,0,.25), 0 0 18px rgba(255,166,0,.30);} 
    .agent-mood.mood-calm{color:#00f5ff; text-shadow:0 1px 0 rgba(0,0,0,.25), 0 0 18px rgba(0,245,255,.28);} 
    .agent-mood.mood-lost{color:#9aa4b2; text-shadow:none;} 
    .agent-mood.mood-playful{color:#ff3e8e; text-shadow:0 1px 0 rgba(0,0,0,.25), 0 0 18px rgba(255,62,142,.28);} 
    .agent-mood.mood-tired{color:#c7cbd1; text-shadow:0 1px 0 rgba(0,0,0,.25);} 

    /* Mood / sentiment color accents */
    .agent-hero{
      /* Ensure the hero fill is painted by this element (no underlying .card gradient). */
      background: color-mix(in srgb, var(--cb-card-bg) 96%, transparent) !important;
      border-color: color-mix(in srgb, var(--cb-border-strong) 55%, var(--claw-accent-a) 25%);
      position:relative; overflow:hidden;
      min-height: 140px;
    }
    /* Ensure inner wrappers never paint a background strip */
    .agent-hero *{background-color: transparent !important;}

    /* Mood fill: set background on the outermost rounded container so it fills all the way to the bottom. */
    .agent-hero.mood-calm{
      background:
        radial-gradient(1200px 360px at 18% 0%, rgba(0,245,255,.20), transparent 62%),
        radial-gradient(1200px 360px at 84% 12%, rgba(123,44,255,.16), transparent 64%),
        linear-gradient(180deg, color-mix(in srgb, var(--cb-card-bg) 96%, transparent), color-mix(in srgb, var(--cb-card-bg) 92%, rgba(0,245,255,.06)));
      box-shadow:0 0 0 1px color-mix(in srgb, var(--claw-accent-a) 22%, transparent), var(--cb-shadow-soft);
    }

    .agent-hero.mood-alert{
      background:
        radial-gradient(1200px 360px at 16% 0%, rgba(255,64,64,.22), transparent 62%),
        radial-gradient(1200px 360px at 82% 12%, rgba(255,62,142,.12), transparent 64%),
        linear-gradient(180deg, color-mix(in srgb, var(--cb-card-bg) 96%, transparent), color-mix(in srgb, var(--cb-card-bg) 90%, rgba(255,64,64,.08)));
      border-color: rgba(255,64,64,.55);
      box-shadow:0 0 0 1px rgba(255,64,64,.55), 0 0 38px rgba(255,64,64,.26), var(--cb-shadow-soft);
    }

    .agent-hero.mood-focused{
      background:
        radial-gradient(1200px 520px at 20% 0%, rgba(181,123,255,.26), transparent 68%),
        radial-gradient(1200px 520px at 84% 12%, rgba(0,245,255,.12), transparent 70%),
        linear-gradient(180deg,
          color-mix(in srgb, var(--cb-card-bg) 94%, rgba(181,123,255,.10)),
          color-mix(in srgb, var(--cb-card-bg) 88%, rgba(181,123,255,.14)));
      background-repeat:no-repeat;
      background-attachment:scroll;
      border-color: rgba(181,123,255,.60);
      box-shadow:0 0 0 1px color-mix(in srgb, var(--claw-accent-b) 38%, transparent), 0 0 34px color-mix(in srgb, var(--claw-accent-b) 26%, transparent), var(--cb-shadow-soft);
    }

    .agent-hero.mood-degraded{
      background:
        radial-gradient(1200px 360px at 18% 0%, rgba(255,166,0,.20), transparent 62%),
        radial-gradient(1200px 360px at 84% 12%, rgba(255,64,64,.08), transparent 64%),
        linear-gradient(180deg, color-mix(in srgb, var(--cb-card-bg) 96%, transparent), color-mix(in srgb, var(--cb-card-bg) 90%, rgba(255,166,0,.08)));
      border-color: rgba(255,166,0,.52);
      box-shadow:0 0 0 1px rgba(255,166,0,.48), 0 0 34px rgba(255,166,0,.20), var(--cb-shadow-soft);
    }

    .agent-hero.mood-lost{
      background:
        radial-gradient(1200px 360px at 18% 0%, rgba(140,150,160,.12), transparent 62%),
        radial-gradient(1200px 360px at 84% 12%, rgba(0,0,0,.06), transparent 64%),
        linear-gradient(180deg, color-mix(in srgb, var(--cb-card-bg) 96%, transparent), color-mix(in srgb, var(--cb-card-bg) 92%, rgba(0,0,0,.04)));
      opacity:0.92; filter:saturate(.92);
    }
    .btn{height:44px;padding:0 16px;border:1px solid var(--cb-border);border-radius:12px;
      background:linear-gradient(135deg,
        color-mix(in srgb, var(--secondary-background-color) 88%, var(--cb-card-bg)),
        color-mix(in srgb, var(--claw-bg-2) 18%, transparent));
      color:var(--primary-text-color);cursor:pointer;display:inline-flex;align-items:center;justify-content:center;gap:8px;
      transition:transform .12s ease, filter .12s ease, box-shadow .12s ease;
      box-shadow:0 0 0 rgba(0,0,0,0);
    }
    .btn:hover{filter:brightness(1.04); box-shadow:0 0 0 1px color-mix(in srgb, var(--claw-accent-a) 25%, transparent), 0 10px 30px rgba(0,0,0,.12);}
    .btn:active{transform:translateY(1px) scale(.99);}
    .btn:disabled{opacity:0.5;cursor:not-allowed;filter:none;transform:none;box-shadow:none;}
    .btn.primary{border-color:color-mix(in srgb, var(--claw-accent-a) 40%, var(--cb-border-strong));
      background:linear-gradient(135deg, var(--claw-accent-a), var(--claw-accent-b));
      color:#081019;box-shadow:0 8px 24px color-mix(in srgb, var(--claw-btn-glow) 60%, transparent);
    }
    .btn.primary:hover{filter:brightness(1.02);}
    .tabs{display:inline-flex;align-items:center;gap:0;margin-top:12px;margin-bottom:14px;
      padding:3px;border-radius:14px;
      background:color-mix(in srgb, var(--secondary-background-color) 92%, var(--cb-card-bg));
      border:1px solid var(--cb-border);
      box-shadow:inset 0 0 0 1px color-mix(in srgb, var(--divider-color) 75%, transparent);
    }
    .tab{height:40px;min-width:96px;padding:0 14px;border:none;border-radius:10px;
      background:transparent;
      color:var(--primary-text-color);
      cursor:pointer;display:flex;flex:0 0 auto;align-items:center;justify-content:center;
      font-weight:700;
    }
    .tab + .tab{border-left:1px solid color-mix(in srgb, var(--divider-color) 70%, transparent);}
    .tab:hover{background:color-mix(in srgb, var(--secondary-background-color) 78%, var(--cb-card-bg));}
    .tab.active{
      background:color-mix(in srgb, var(--mdc-theme-primary, var(--primary-color)) 18%, var(--cb-card-bg));
      color:var(--primary-text-color);
      position:relative;
      box-shadow:inset 0 0 0 1px var(--cb-border-strong);
    }
    .tab.active::after{
      content:"";position:absolute;left:12px;right:12px;bottom:6px;height:3px;border-radius:999px;
      background:linear-gradient(90deg, rgba(0,245,255,.95), rgba(123,44,255,.95));
    }
    @media (prefers-reduced-motion: reduce){ .tab, .btn { transition:none !important; } }
    .hidden{display:none;}
    .kv{display:flex;gap:10px;flex-wrap:wrap;margin-top:10px;}
    .kv > div{background:color-mix(in srgb, var(--secondary-background-color) 90%, var(--cb-card-bg));border:1px solid var(--cb-border);border-radius:10px;padding:8px 10px;}
    .pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;border:1px solid var(--cb-border);background:var(--secondary-background-color);color:var(--primary-text-color);}
    .pill.ok{border-color:var(--success-color, #2e7d32);background:color-mix(in srgb, var(--success-color, #2e7d32) 15%, transparent);color:var(--success-color, #2e7d32);}
    .pill.bad{border-color:var(--error-color, #b00020);background:color-mix(in srgb, var(--error-color, #b00020) 15%, transparent);color:var(--error-color, #b00020);}
    .entities{max-height:420px;overflow:auto;border:1px solid var(--cb-border-strong);border-radius:10px;padding:10px;box-shadow:inset 0 0 0 1px color-mix(in srgb, var(--divider-color) 70%, transparent);}
    .grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;}
    .setup-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;align-items:start;}
    @media (max-width: 860px){ .setup-grid{grid-template-columns:1fr;} }

    /* Mobile responsiveness */
    @media (max-width: 600px){
      body{padding:10px;}
      .surface{padding:14px; max-width:100%;}
      .card{padding:14px; margin:12px 0;}
      h1{font-size:20px;}
      h2{font-size:16px;}

      /* Tabs: allow horizontal scroll instead of squish */
      .tabs{display:flex; overflow-x:auto; -webkit-overflow-scrolling:touch; max-width:100%;}
      .tab{min-width:84px; height:38px; padding:0 10px;}

      /* Pills/chips tighter */
      .pill{font-size:11px; padding:2px 7px;}

      /* Agent hero: tighten typography */
      .agent-title{font-size:24px;}
      .agent-desc{font-size:14px;}
      .agent-mood{font-size:12px;}

      /* Visualizer: move to top-right corner of hero on mobile */
      #agentVizWrap{width:72px !important; height:72px !important; position:absolute !important; top:12px !important; right:12px !important; left:auto !important; z-index:6 !important;}
      #agentViz{width:72px !important; height:72px !important;}

      /* STT widget legacy float is hidden */
      #agentSttFloat{display:none !important;}
      /* Header transcript row becomes stacked on mobile */
      #appSubtitleRow{flex-wrap:wrap !important;}
      #appTagline{flex:1 1 100% !important;}
      #transcript{flex:1 1 100% !important;text-align:left !important;}

      /* Chat bubbles more width */
      .chat-bubble{max-width:88%;}
    }
    .ent{display:flex;gap:10px;align-items:center;justify-content:space-between;border-bottom:1px solid color-mix(in srgb, var(--divider-color) 90%, transparent);padding:7px 0;}
    .ent:last-child{border-bottom:none;}
    .ent-id{font-weight:650;}
    .ent-state{color:var(--secondary-text-color);}
    .suggest-card{border:1px solid var(--cb-border);border-radius:12px;padding:12px;background:color-mix(in srgb, var(--secondary-background-color) 90%, var(--cb-card-bg));box-shadow:0 2px 8px rgba(0,0,0,.08);}
    .choice{display:flex;gap:8px;align-items:flex-start;padding:4px 0;}
    .choice input{margin-top:3px;}
    .choice-main{font-size:13px;}
    .choice-meta{font-size:12px;color:var(--secondary-text-color);}
    .chat-shell{display:flex;flex-direction:column;height:min(68vh,720px);min-height:0;border:1px solid var(--cb-border-strong);border-radius:16px;background:var(--cb-card-bg);box-shadow:0 8px 18px rgba(0,0,0,.1);overflow:hidden;}
    .chat-list{flex:1;min-height:0;overflow:auto;padding:0 16px 16px 16px;position:relative;background:linear-gradient(180deg, color-mix(in srgb, var(--secondary-background-color) 90%, transparent) 0%, transparent 65%);} 
    .chat-stack{display:flex;flex-direction:column;gap:12px;min-height:100%;justify-content:flex-end;}
    .chat-row{display:flex;align-items:flex-end;gap:10px;}
    .chat-row.user{justify-content:flex-end;}
    .chat-row.agent{justify-content:flex-start;}
    .chat-bubble{max-width:72%;padding:12px 14px;border-radius:16px;border:1px solid var(--cb-border);background:var(--secondary-background-color);box-shadow:0 6px 14px rgba(0,0,0,.06);white-space:pre-wrap;}
    .chat-row.user .chat-bubble{background:var(--mdc-theme-primary, var(--primary-color));border-color:var(--mdc-theme-primary, var(--primary-color));color:#fff;}
    .chat-row.agent .chat-bubble{background:var(--cb-card-bg);border-color:var(--cb-border-strong);color:var(--primary-text-color);}
    .chat-meta{font-size:12px;color:var(--secondary-text-color);margin-top:6px;display:flex;gap:8px;align-items:center;justify-content:space-between;}
    .chat-input{display:flex;gap:10px;padding:12px;border-top:1px solid var(--cb-border);background:color-mix(in srgb, var(--secondary-background-color) 92%, var(--cb-card-bg));box-shadow:0 -10px 30px rgba(0,0,0,.08);}
    .chat-input input{flex:1;min-width:220px;height:46px;}
    .chat-bubble pre{margin:8px 0 0 0;padding:10px 12px;border-radius:12px;background:color-mix(in srgb, var(--primary-background-color) 70%, var(--cb-card-bg));border:1px solid var(--cb-border);overflow:auto;}
    .chat-bubble code{background:color-mix(in srgb, var(--primary-background-color) 78%, var(--cb-card-bg));padding:2px 6px;border-radius:8px;}
    .chat-head{display:flex;justify-content:space-between;align-items:flex-end;gap:10px;margin:0 0 6px 0;}
    .chat-head-left{display:flex;flex-direction:column;gap:3px;}
    .chat-head-right{display:flex;align-items:center;gap:10px;}
    .select{
      background:var(--cb-control-bg);
      color:var(--primary-text-color);
      appearance:auto;
      color-scheme: light dark;
    }
    select option,.select option{background:var(--cb-card-bg);color:var(--primary-text-color);}
    .chat-session{height:40px;min-width:180px;max-width:520px;width:52vw;flex:1;}
    .chat-load-top{display:flex;justify-content:center;margin:0 0 10px 0;}
    .chat-load-top .btn{height:32px;font-size:12px;padding:0 12px;border-radius:999px;background:color-mix(in srgb, var(--ha-card-background, var(--card-background-color)) 70%, transparent);}
    @media (max-width: 680px){ .chat-bubble{max-width:90%;} .chat-shell{height:72vh;} }

    /* Entity configuration (Setup) */
    .cfg-row{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:10px 0;border-top:1px solid var(--divider-color);}
    .cfg-row:first-child{border-top:none;}
    .cfg-left{min-width:0;display:flex;flex-direction:column;gap:4px;flex:1;}
    .cfg-label{font-weight:600;}
    .cfg-selected{min-width:0;display:flex;flex-direction:column;gap:2px;padding:8px 10px;border-radius:12px;border:1px solid color-mix(in srgb, var(--divider-color) 70%, transparent);background:color-mix(in srgb, var(--ha-card-background, var(--card-background-color)) 92%, transparent);}
    .cfg-name{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
    .cfg-meta{font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; font-size:12px; color: var(--secondary-text-color); white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
    .cfg-actions{display:flex;gap:8px;flex-shrink:0;}
    @media (max-width: 680px){ .cfg-row{flex-direction:column;align-items:stretch;} .cfg-actions{justify-content:flex-end;} }

    /* Modal */
    .modal{position:fixed;inset:0;background:rgba(0,0,0,0.35);display:flex;align-items:center;justify-content:center;z-index:9999;}
    .modal.hidden{display:none;}
    .modal-card{width:min(760px,92vw);max-height:min(72vh,720px);overflow:auto;background:var(--ha-card-background, var(--card-background-color));border-radius:16px;box-shadow:0 20px 60px rgba(0,0,0,0.25);padding:18px;}
    .picker-list{display:flex;flex-direction:column;gap:6px;}
    .pick-item{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:10px 12px;border-radius:12px;border:1px solid color-mix(in srgb, var(--divider-color) 70%, transparent);background:color-mix(in srgb, var(--ha-card-background, var(--card-background-color)) 90%, transparent);cursor:pointer;}
    .pick-item:hover{border-color: color-mix(in srgb, var(--primary-color) 55%, var(--divider-color));}
    .pick-main{display:flex;flex-direction:column;min-width:0;}
    .pick-name{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
    .pick-meta{font-size:12px;color:var(--secondary-text-color);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}

    /* Toast */
    .toast{position:fixed;left:50%;transform:translateX(-50%);bottom:18px;z-index:10000;max-width:min(720px,92vw);background:color-mix(in srgb, var(--ha-card-background, var(--card-background-color)) 96%, black);border:1px solid color-mix(in srgb, var(--divider-color) 70%, transparent);box-shadow:0 16px 46px rgba(0,0,0,0.2);border-radius:999px;padding:10px 14px;color:var(--primary-text-color);font-size:13px;}
    .toast.hidden{display:none;}

    /* Kill giant default radio circles if any legacy suggestion UI remains */
    .choice input[type=radio]{display:none;}
  </style>
</head>
<body>
  <div class=\"surface\">
  <div class=\"row\" id=\"appHeaderRow\" style=\"justify-content:space-between;align-items:center;gap:12px;flex-wrap:nowrap\">
    <h1 id=\"appTitle\" style=\"margin:0;flex:1 1 auto;min-width:0\">Hello, this is Agent 0</h1>
    <div style=\"flex:0 0 auto;display:flex;gap:8px;align-items:center\">
      <button class=\"btn primary\" id=\"btnListen\" style=\"height:34px;border-radius:12px;padding:0 12px\">Listen</button>
      <button class=\"btn\" id=\"btnStopListen\" disabled style=\"display:none;height:34px;border-radius:12px;padding:0 12px\">Stop</button>
    </div>
  </div>
  <div class=\"muted\" id=\"debugStamp\" style=\"display:none;margin:6px 0 0 0\"></div>
  <div class=\"row\" id=\"appSubtitleRow\" style=\"justify-content:space-between;align-items:center;gap:10px;flex-wrap:nowrap;margin:0 0 10px 0;\">
    <div class=\"muted\" id=\"appTagline\" style=\"margin:0;flex:1 1 auto;min-width:0\"></div>
    <div class=\"muted\" id=\"listenStatus\" style=\"display:none\"></div>
    <div id=\"transcript\" style=\"flex:0 1 46%;min-width:0;text-align:right;max-height:20px;overflow:hidden;padding:0;background:transparent;border:none;white-space:nowrap;text-overflow:ellipsis;font-size:12px;font-weight:800;color:#25d366\"></div>
  </div>

  <script type=\"application/json\" id=\"clawdbot-config\">__CONFIG_JSON__</script>
  <script src=\"/local/clawdbot-panel.js?v=__PANEL_BUILD_ID__\"></script>
  </script>

  <div class=\"tabs\" style=\"background:linear-gradient(135deg, color-mix(in srgb, var(--claw-bg-1) 80%, transparent), color-mix(in srgb, var(--claw-bg-2) 75%, transparent));border:1px solid color-mix(in srgb, var(--cb-border) 60%, var(--claw-accent-a) 12%);box-shadow:0 14px 34px rgba(0,0,0,.10);backdrop-filter: blur(10px);" >
    <button type=\"button\" class=\"tab active\" id=\"tabAgent\">Agent</button>
    <button type=\"button\" class=\"tab\" id=\"tabCockpit\">Cockpit</button>
    <button type=\"button\" class=\"tab\" id=\"tabChat\">Chat</button>
    <button type=\"button\" class=\"tab\" id=\"tabSetup\">Setup</button>
  </div>

  <div id=\"viewSetup\" class=\"hidden\">
    <div class=\"setup-grid\">
    <div class=\"card\">
      <h2>Commissioning</h2>
      <div class=\"muted\">Verify configuration and connectivity before using the cockpit.</div>
      <div class=\"kv\" id=\"cfgSummary\"></div>
      <div class=\"muted\" id=\"buildInfo\" style=\"margin-top:8px\"></div>
      <div style=\"margin-top:14px\">
        <h2 style=\"margin:0 0 8px 0;font-size:15px\">Theme</h2>
        <div class=\"muted\" style=\"margin-bottom:8px\">Pick a preset theme (affects background, cards, buttons). Optional auto-mode lets the agent shift themes based on “mood”.</div>
        <div class=\"row\">
          <select id=\"themePreset\" class=\"select\" style=\"min-width:240px\"></select>
          <label class=\"muted\" style=\"display:flex;align-items:center;gap:8px\">
            <input type=\"checkbox\" id=\"themeAuto\"/>
            Auto (mood)
          </label>
          <button class=\"btn primary\" id=\"btnThemeApply\">Apply</button>
          <button class=\"btn\" id=\"btnThemeReset\">Reset</button>
          <span class=\"muted\" id=\"themeResult\"></span>
        </div>
        <div id=\"themePreview\" style=\"margin-top:10px;height:64px;border-radius:16px;border:1px solid var(--divider-color);background:linear-gradient(120deg, rgba(0,245,255,.18), rgba(123,44,255,.18)), color-mix(in srgb, var(--ha-card-background, var(--card-background-color)) 85%, transparent)\"></div>
      </div>

      <div style=\"margin-top:14px\">
        <h2 style=\"margin:0 0 8px 0;font-size:15px\">Connection overrides</h2>
        <div class=\"muted\" style=\"margin-bottom:8px\">Edit gateway_url/token/session key. Save/Apply persists to <code>.storage</code>. Reset clears overrides.</div>
        <div class=\"row\">
          <input id=\"connGatewayUrl\" style=\"flex:1;min-width:260px\" placeholder=\"gateway_url (e.g. http://host:7773)\"/>
          <input id=\"connSessionKey\" style=\"flex:1;min-width:220px\" placeholder=\"session_key (e.g. main)\"/>
        </div>
        <div class=\"row\" style=\"margin-top:8px\">
          <input id=\"connToken\" type=\"password\" style=\"flex:1;min-width:260px\" placeholder=\"token (stored locally)\"/>
        </div>
        <div class=\"row\" style=\"margin-top:8px\">
          <button class=\"btn primary\" id=\"btnConnSave\">Save/Apply</button>
          <button class=\"btn\" id=\"btnConnReset\">Reset to YAML defaults</button>
          <span class=\"muted\" id=\"connResult\" style=\"min-width:180px;display:inline-block\"></span>
        </div>
      </div>
      <div class=\"row\" style=\"margin-top:10px\">
        <button class=\"btn primary\" id=\"btnGatewayTest\">Test gateway</button>
        <span class=\"muted\" id=\"gwTestResult\"></span>
      </div>

      <div style=\"margin-top:14px\">
        <h2 style=\"margin:0 0 8px 0;font-size:15px\">Dynamic setup options</h2>
        <div class=\"muted\" style=\"margin-bottom:8px\">Data-driven options registry (agent-definable). Values persist to <code>.storage</code>. Secrets are masked.</div>
        <div id=\"setupOptions\" class=\"muted\">Loading…</div>
      </div>

      <div style=\"margin-top:14px\">
        <div class=\"muted\" style=\"margin-bottom:8px\">Send test inbound event (calls <code>clawdbot.notify_event</code>):</div>
        <div class=\"row\">
          <input id=\"evtType\" placeholder=\"event_type\" value=\"clawdbot.test\"/>
          <select id=\"evtSeverity\" class=\"select\">
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
      <h2>Entity configuration</h2>
      <div class=\"muted\">Pick the 4 signals the Cockpit uses. We auto-suggest; click Select to search (no giant lists).</div>

      <div class=\"row\" style=\"margin-top:12px;justify-content:space-between;align-items:center\">
        <div class=\"muted\">Suggestions will prefill automatically when empty. Review then confirm.</div>
        <div class=\"row\" style=\"gap:8px\">
          <button class=\"btn primary\" id=\"btnConfirmAll\">Confirm all</button>
          <span class=\"muted\" id=\"confirmAllResult\"></span>
        </div>
      </div>

      <div id=\"entityConfig\" style=\"margin-top:10px\">
        <div class=\"cfg-row\" data-field=\"soc\">
          <div class=\"cfg-left\">
            <div class=\"cfg-label\">Battery SOC</div>
            <div class=\"cfg-selected\">
              <div class=\"cfg-name\" id=\"cfgSocName\">Not set</div>
              <div class=\"cfg-meta\" id=\"cfgSocMeta\">—</div>
            </div>
          </div>
          <div class=\"cfg-actions\">
            <button class=\"btn\" data-pick=\"soc\">Select…</button>
            <button class=\"btn\" data-clear=\"soc\">Clear</button>
          </div>
        </div>

        <div class=\"cfg-row\" data-field=\"voltage\">
          <div class=\"cfg-left\">
            <div class=\"cfg-label\">Battery Voltage</div>
            <div class=\"cfg-selected\">
              <div class=\"cfg-name\" id=\"cfgVoltageName\">Not set</div>
              <div class=\"cfg-meta\" id=\"cfgVoltageMeta\">—</div>
            </div>
          </div>
          <div class=\"cfg-actions\">
            <button class=\"btn\" data-pick=\"voltage\">Select…</button>
            <button class=\"btn\" data-clear=\"voltage\">Clear</button>
          </div>
        </div>

        <div class=\"cfg-row\" data-field=\"solar\">
          <div class=\"cfg-left\">
            <div class=\"cfg-label\">Solar Power</div>
            <div class=\"cfg-selected\">
              <div class=\"cfg-name\" id=\"cfgSolarName\">Not set</div>
              <div class=\"cfg-meta\" id=\"cfgSolarMeta\">—</div>
            </div>
          </div>
          <div class=\"cfg-actions\">
            <button class=\"btn\" data-pick=\"solar\">Select…</button>
            <button class=\"btn\" data-clear=\"solar\">Clear</button>
          </div>
        </div>

        <div class=\"cfg-row\" data-field=\"load\">
          <div class=\"cfg-left\">
            <div class=\"cfg-label\">Load Power</div>
            <div class=\"cfg-selected\">
              <div class=\"cfg-name\" id=\"cfgLoadName\">Not set</div>
              <div class=\"cfg-meta\" id=\"cfgLoadMeta\">—</div>
            </div>
          </div>
          <div class=\"cfg-actions\">
            <button class=\"btn\" data-pick=\"load\">Select…</button>
            <button class=\"btn\" data-clear=\"load\">Clear</button>
          </div>
        </div>
      </div>

      <details style=\"margin-top:12px\">
        <summary class=\"muted\">Advanced (raw entity_id)</summary>
        <div class=\"muted\" style=\"margin-top:8px\">Only if the picker can’t find your entity.</div>
        <div class=\"row\" style=\"margin-top:8px\">
          <input list=\"entityIdList\" id=\"mapSoc\" style=\"flex:1;min-width:220px\" placeholder=\"soc entity_id\"/>
          <input list=\"entityIdList\" id=\"mapVoltage\" style=\"flex:1;min-width:220px\" placeholder=\"voltage entity_id\"/>
        </div>
        <div class=\"row\" style=\"margin-top:8px\">
          <input list=\"entityIdList\" id=\"mapSolar\" style=\"flex:1;min-width:220px\" placeholder=\"solar power entity_id\"/>
          <input list=\"entityIdList\" id=\"mapLoad\" style=\"flex:1;min-width:220px\" placeholder=\"load/consumption entity_id\"/>
        </div>
        <div class=\"row\" style=\"margin-top:8px\">
          <button class=\"btn primary\" id=\"btnMapSaveAdvanced\">Save advanced mapping</button>
          <span class=\"muted\" id=\"mapSaveAdvancedResult\"></span>
        </div>
      </details>

      <datalist id=\"entityIdList\"></datalist>

      <div id=\"toast\" class=\"toast hidden\"></div>

      <div id=\"pickerModal\" class=\"modal hidden\">
        <div class=\"modal-card\">
          <div class=\"row\" style=\"justify-content:space-between\">
            <div><b id=\"pickerTitle\">Select entity</b></div>
            <button class=\"btn\" id=\"pickerClose\">Close</button>
          </div>
          <input id=\"pickerSearch\" placeholder=\"Search entities…\" style=\"margin-top:10px;width:100%\"/>
          <div class=\"muted\" id=\"pickerHint\" style=\"margin-top:6px\"></div>
          <div id=\"pickerList\" class=\"picker-list\" style=\"margin-top:10px\"></div>
        </div>
      </div>
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

    <div class=\"card\" id=\"suggestedSensorsCard\">
      <h2>Suggested sensors (virtual)</h2>
      <div class=\"muted\">One-click derived sensors based on your mapped solar/load signals. Stored enablement persists across restarts.</div>
      <div class=\"row\" style=\"margin-top:10px;justify-content:space-between;align-items:center\">
        <div class=\"row\" style=\"gap:8px\">
          <button class=\"btn primary\" id=\"btnDerivedEnable\">Create / Enable</button>
          <button class=\"btn\" id=\"btnDerivedDisable\">Disable</button>
        </div>
        <span class=\"muted\" id=\"derivedStatus\"></span>
      </div>
      <div id=\"suggestedSensorsList\" style=\"margin-top:10px\"><div class=\"muted\">Loading…</div></div>
    </div>

    <div class=\"card\" id=\"statusCard\">
      <div class=\"row\">
        <div class=\"row\"><div><b>Status:</b> <span id=\"status\">checking…</span></div><span id=\"connPill\" class=\"pill\">…</span></div>
        <button class=\"btn\" id=\"refreshBtn\">Refresh entities</button>
      </div>
      <div class=\"muted\" id=\"statusDetail\"></div>
      <div class="muted" id="statusHint" style="margin-top:6px"></div>
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

  <div id=\"viewAgent\" class=\"hidden\">
    <!-- Voice-to-text control (desktop floats top-right; mobile becomes in-flow via CSS) -->
    <div id=\"agentSttFloat\" style=\"display:none;position:fixed;top:calc(env(safe-area-inset-top, 0px) + 72px);right:14px;z-index:50;max-width:min(420px,calc(100vw - 28px));\">
      <div style=\"margin:0;padding:0;background:transparent;border:none;\">
        <div class=\"row\" style=\"justify-content:flex-end;align-items:center;gap:10px;flex-wrap:nowrap\">
          <button class=\"btn primary\" id=\"btnListenOld\" style=\"height:36px;border-radius:12px;padding:0 12px;flex:0 0 auto\">Listen</button>
          <button class=\"btn\" id=\"btnStopListenOld\" disabled style=\"display:none;height:36px;border-radius:12px;padding:0 12px\">Stop</button>
        </div>
        <div class=\"muted\" id=\"listenStatusOld\" style=\"display:none\"></div>
        <div id=\"transcriptOld\" style=\"margin-top:6px;max-height:40px;overflow:hidden;padding:0;background:transparent;border:none;white-space:nowrap;text-overflow:ellipsis;font-size:12px;font-weight:800;color:#25d366\"></div>
      </div>
    </div>

    <div class=\"card agent-hero\" id=\"agentHeroCard\" style=\"position:relative;overflow:hidden\">
      <div style=\"position:absolute;inset:0;background:radial-gradient(circle at 20% 30%, rgba(0,245,255,.22), transparent 60%), radial-gradient(circle at 70% 40%, rgba(123,44,255,.20), transparent 65%);filter:blur(0px);pointer-events:none;z-index:1\"></div>
      <div class=\"row\" style=\"position:relative;z-index:1;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap\">
        <div class=\"row\" style=\"gap:14px;align-items:center\">
          <button type=\"button\" id=\"agentAvatarBtn\" class=\"btn\" style=\"width:128px;height:128px;border-radius:28px;display:flex;align-items:center;justify-content:center;overflow:hidden;background:linear-gradient(135deg, rgba(0,245,255,.25), rgba(123,44,255,.25));border:1px solid color-mix(in srgb, var(--primary-color) 45%, var(--divider-color));font-weight:800;letter-spacing:.5px;font-size:28px;cursor:pointer;position:relative;padding:0\">\n            <img id=\"agentAvatarImg\" alt=\"agent avatar\" style=\"display:none;width:100%;height:100%;object-fit:cover\"/>\n            <div id=\"agentAvatarFallback\" style=\"display:flex;align-items:center;justify-content:center;width:100%;height:100%\">A0</div>\n          </button>\n          <div id=\"avatarGenModal\" class=\"modal hidden\" style=\"position:fixed;inset:0;background:rgba(0,0,0,0.45);display:none;align-items:center;justify-content:center;z-index:10000;\">\n            <div class=\"modal-card\" style=\"max-width:720px;width:min(720px,92vw);max-height:min(78vh,720px);overflow:auto;color:var(--primary-text-color);border-radius:20px;padding:22px;border:1px solid color-mix(in srgb, var(--claw-accent-a) 45%, transparent);box-shadow:0 26px 80px rgba(0,0,0,0.55);background:linear-gradient(135deg, color-mix(in srgb, var(--cb-card-bg) 92%, var(--claw-accent-a) 10%), color-mix(in srgb, var(--cb-card-bg) 92%, var(--claw-accent-b) 10%));\">\n              <div style=\"display:flex;justify-content:space-between;align-items:center;gap:12px\">\n                <div style=\"font-weight:950;letter-spacing:-0.02em;font-size:18px\">Describe your agent</div>\n                <button class=\"btn\" id=\"avatarGenClose\" title=\"Close\" style=\"width:38px;height:38px;border-radius:12px;padding:0;display:flex;align-items:center;justify-content:center;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.10);\">✕</button>\n              </div>\n              <div class=\"muted\" style=\"margin-top:8px;line-height:1.35;color:var(--secondary-text-color)\">Describe what your agent looks like. Or hit <b>Surprise me</b> to auto-draft a backstory + appearance.</div>\n              <textarea id=\"avatarGenText\" style=\"margin-top:12px;width:100%;min-height:120px;resize:vertical;border-radius:16px;border:1px solid color-mix(in srgb, var(--claw-accent-a) 25%, var(--cb-border-strong));padding:12px 24px 12px 18px;background:color-mix(in srgb, var(--cb-card-bg) 86%, #000);color:var(--primary-text-color);font-family:inherit;outline:none;box-sizing:border-box\" placeholder=\"e.g., Warm smile, short black hair, futuristic pilot jacket...\"></textarea>\n              <div id=\"avatarGenPreviewWrap\" style=\"margin-top:12px;display:flex;gap:12px;align-items:center;justify-content:space-between\">\n                <div style=\"display:flex;gap:12px;align-items:center\">\n                  <div style=\"position:relative;width:96px;height:96px\">\n                    <img id=\"avatarGenPreviewImg\" alt=\"avatar preview\" style=\"width:96px;height:96px;border-radius:18px;object-fit:cover;border:1px solid color-mix(in srgb, var(--claw-accent-a) 30%, var(--cb-border-strong));background:color-mix(in srgb, var(--cb-card-bg) 86%, #000);display:none\"/>\n                    <div id=\"avatarGenPreviewStatus\" class=\"muted\" style=\"position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;padding:10px;border-radius:18px;border:1px dashed color-mix(in srgb, var(--claw-accent-a) 28%, var(--cb-border-strong));background:color-mix(in srgb, var(--cb-card-bg) 75%, transparent);font-size:12px;color:var(--secondary-text-color)\">No preview yet</div>\n                  </div>\n                  <div class=\"muted\" style=\"font-size:12px;color:var(--secondary-text-color)\">Preview for this run. Click <b>Use this</b> to apply as your profile avatar.</div>\n                </div>\n                <button class=\"btn\" id=\"avatarGenUse\" data-testid=\"avatar-use\" style=\"height:34px;border-radius:12px;padding:0 12px;background:color-mix(in srgb, var(--claw-accent-a) 18%, var(--cb-card-bg));border:1px solid color-mix(in srgb, var(--claw-accent-a) 40%, var(--cb-border-strong));color:var(--primary-text-color);font-weight:800\">Use this</button>\n              </div>\n              <div class=\"row\" style=\"justify-content:flex-end;gap:10px;margin-top:12px;flex-wrap:wrap\">\n                <button class=\"btn\" id=\"avatarGenCancel\" data-testid=\"avatar-cancel\" style=\"display:none;height:38px;border-radius:14px;padding:0 14px;background:color-mix(in srgb, var(--cb-card-bg) 80%, transparent);border:1px solid color-mix(in srgb, var(--cb-border-strong) 80%, transparent);color:var(--primary-text-color);\">Cancel</button>\n                <button class=\"btn\" id=\"avatarGenSurprise\" data-testid=\"avatar-surprise\" style=\"height:38px;border-radius:14px;padding:0 14px;background:color-mix(in srgb, var(--cb-card-bg) 80%, transparent);border:1px solid color-mix(in srgb, var(--claw-accent-a) 35%, var(--cb-border-strong));color:var(--primary-text-color);\">Surprise me</button>\n                <button class=\"btn primary\" id=\"avatarGenGenerate\" data-testid=\"avatar-generate\" style=\"height:38px;border-radius:14px;padding:0 16px;border:1px solid color-mix(in srgb, var(--claw-accent-a) 55%, transparent);background:linear-gradient(135deg, color-mix(in srgb, var(--claw-accent-a) 85%, #fff 0%), color-mix(in srgb, var(--claw-accent-b) 85%, #fff 0%));color:#061018;font-weight:900;\">Generate</button>\n              </div>\n              <div class=\"muted\" id=\"avatarGenHint\" style=\"margin-top:10px;font-size:12px;color:var(--secondary-text-color);white-space:pre-line\"></div>\n              <div class=\"muted\" id=\"avatarGenStage\" style=\"margin-top:6px;font-size:11px;opacity:.75;color:var(--secondary-text-color)\"></div>\n              <div class=\"muted\" id=\"avatarGenDebug\" style=\"display:none;margin-top:8px;font-size:11px;opacity:.65;color:var(--secondary-text-color)\"></div>\n            </div>\n          </div>
          <div style=\"display:flex;flex-direction:column;gap:4px;min-width:260px\">
            <div class=\"agent-title\">Agent 0 <span class=\"agent-mood\" id=\"agentMood\">· mood: calm</span></div>
            <div class=\"agent-desc\" id=\"agentDesc\">Ship ops / energy monitoring assistant</div>
            <div class=\"muted\" id=\"agentMeta\" style=\"font-size:11px\"></div>
            <div class=\"muted\" id=\"agentLiveMeta\" style=\"font-size:11px\"></div>
            <div class=\"row\" style=\"gap:8px;flex-wrap:wrap\">
              <span class=\"pill\" id=\"agentConnPill\">…</span>
              <span class=\"pill\" id=\"agentDerivedPill\">…</span>
              <span class=\"pill\" id=\"agentSessionPill\">session: —</span>
              <span class=\"muted\" id=\"agentUptime\" style=\"margin-left:6px\">uptime: —</span>
            </div>
          </div>
        </div>
        <div class=\"row\" style=\"gap:10px;align-items:center\">
          <div id=\"agentVizWrap\" style=\"width:96px;height:96px;position:relative;flex:0 0 auto\">
            <canvas id=\"agentViz\" width=\"96\" height=\"96\" style=\"width:96px;height:96px;display:block\"></canvas>
          </div>
        </div>
      </div>
    </div>

    <div class=\"card\">
      <h2 style=\"display:flex;justify-content:space-between;align-items:center\">Live activity <span class=\"muted\" style=\"font-size:12px\">last 5</span></h2>
      <div id=\"agentActivity\" class=\"muted\">No activity yet.</div>
    </div>

    <div class=\"card\">
      <h2>Journal</h2>
      <div class=\"muted\">Agent journals (mood-aware). Mirrors what gets posted to the Discord journal channel.</div>
      <div id=\"agentJournal\" class=\"muted\" style=\"margin-top:10px\">No journal entries yet.</div>
    </div>

    <!-- STT widget moved to top of Agent view (see above) -->
  </div>

  <div id=\"viewChat\" class=\"hidden\">
    <div class=\"chat-head\">
      <div class=\"chat-head-left\">
        <span class=\"muted\" style=\"font-size:12px\">Session</span>
        <div class=\"row\" style=\"gap:8px;align-items:center;flex-wrap:nowrap;\">
          <select id=\"chatSessionSelect\" class=\"select chat-session\"></select>
          <button class=\"btn\" id=\"chatNewSessionBtn\" style=\"height:40px;border-radius:12px;padding:0 12px;white-space:nowrap;\">New session</button>
        </div>
      </div>
      <div class=\"chat-head-right\">
        <span class=\"muted\" style=\"font-size:12px\">Tokens: <span id=\"chatTokenUsage\">—</span></span>
        <span id=\"chatPollDebug\" class=\"muted\" style=\"font-size:12px;display:none\"></span>
      </div>
    </div>
    <div class=\"chat-load-top\" id=\"chatLoadTop\">
      <button class=\"btn\" id=\"chatLoadOlderBtn\">Load older</button>
    </div>
    <div class=\"chat-shell\">
      <div id=\"chatList\" class=\"chat-list\"></div>
      <div id=\"chatTyping\" class=\"muted\" style=\"font-size:12px;padding:6px 12px;min-height:20px;line-height:20px\"></div>\n      <div class=\"chat-input\">
        <input id=\"chatComposer\" placeholder=\"Ask Clawdbot…\"/>
        <button class=\"btn primary\" id=\"chatComposerSend\" style=\"min-width:96px\">Send</button>
      </div>
    </div>
  </div>
  </div>


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

        hass = request.app["hass"]
        cfg = hass.data.get(DOMAIN, {})
        rt = _runtime(hass)
        chat_history = cfg.get("chat_history", []) or []
        if not isinstance(chat_history, list):
            chat_history = []
        session_key = rt.get("session_key") or DEFAULT_SESSION_KEY
        session_items = [it for it in chat_history if isinstance(it, dict) and it.get("session_key") == session_key]
        if not session_items:
            session_items = [it for it in chat_history if isinstance(it, dict)]
        chat_history = session_items[-50:]
        chat_has_older = len(session_items) > len(chat_history)
        mapping = cfg.get("mapping", {})
        if not isinstance(mapping, dict):
            mapping = {}

        # First-run gating flags (panel uses these to decide whether to show wizard)
        essentials_missing = not bool(rt.get("gateway_url") or rt.get("gateway_origin")) or not bool(rt.get("token"))
        mapping_missing = any(not mapping.get(k) for k in ("soc", "voltage", "solar", "load"))

        safe_cfg = {
            "build_id": PANEL_BUILD_ID,
            "gateway_url": rt.get("gateway_url") or rt.get("gateway_origin"),
            "has_token": bool(rt.get("token")),
            "session_key": rt.get("session_key") or DEFAULT_SESSION_KEY,
            "mapping": mapping,
            "essentials_missing": essentials_missing,
            "mapping_missing": mapping_missing,
            "house_memory": cfg.get("house_memory", {}),
            "chat_history": chat_history,
            "chat_history_has_older": chat_has_older,
            "theme": cfg.get("theme", {}),
            "journal": (cfg.get("journal", []) or [])[-20:],
            "agent_profile": cfg.get("agent_profile", {}),
        }
        html = PANEL_HTML.replace("__CONFIG_JSON__", dumps(safe_cfg)).replace("__PANEL_BUILD_ID__", PANEL_BUILD_ID)
        return web.Response(
            text=html,
            content_type="text/html",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

class ClawdbotPanelJsView(HomeAssistantView):
    """Serves the panel JS as an external script (CSP-safe)."""

    url = "/clawdbot-panel.js"
    name = "api:clawdbot:panel_js"
    requires_auth = False

    async def get(self, request):
        from aiohttp import web

        return web.Response(
            text=PANEL_JS,
            content_type="application/javascript",
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


class ClawdbotSttWhisperApiView(HomeAssistantView):
    """Same-origin authenticated STT: browser mic → OpenAI Whisper."""

    url = "/api/clawdbot/stt_whisper"
    name = "api:clawdbot:stt_whisper"
    requires_auth = True

    async def _unauthorized(self):
        from aiohttp import web

        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    async def post(self, request):
        from aiohttp import web
        from aiohttp import FormData
        import time

        # Auth guard: return JSON on 401 so panel can display a friendly error
        try:
            if not getattr(request, "user", None) or not request.user.is_authenticated:
                return await self._unauthorized()
        except Exception:
            return await self._unauthorized()

        hass = request.app["hass"]
        cfg = hass.data.get(DOMAIN, {})

        # Rate limit (very basic)
        now = time.time()
        last = float(cfg.get("_stt_last_ts") or 0)
        if now - last < 1.0:
            return web.json_response({"ok": False, "error": "rate_limited"}, status=429)
        cfg["_stt_last_ts"] = now

        # Size cap (bytes)
        max_bytes = 5 * 1024 * 1024
        try:
            raw = await request.read()
        except Exception:
            return web.json_response({"ok": False, "error": "read_failed"}, status=400)
        if not raw:
            return web.json_response({"ok": False, "error": "empty"}, status=400)
        if len(raw) > max_bytes:
            return web.json_response({"ok": False, "error": "too_large"}, status=413)

        # Load OpenAI key from dynamic setup options
        opts = cfg.get("setup_options")
        api_key = None
        if isinstance(opts, dict):
            opt = opts.get("stt.whisper_openai_api_key")
            if isinstance(opt, dict):
                v = opt.get("value")
                if isinstance(v, str) and v.strip():
                    api_key = v.strip()
        if not api_key:
            return web.json_response({"ok": False, "error": "not_configured"}, status=501)

        # Determine filename/content-type
        ct = request.content_type or "application/octet-stream"
        filename = "audio.webm" if "webm" in ct else "audio.wav"

        form = FormData()
        form.add_field("file", raw, filename=filename, content_type=ct)
        form.add_field("model", "whisper-1")

        # Optional language hint
        try:
            q = request.query
            lang = q.get("language") if q else None
            if isinstance(lang, str) and lang.strip():
                form.add_field("language", lang.strip()[:16])
        except Exception:
            pass

        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(hass)
        try:
            resp = await session.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                data=form,
                timeout=30,
            )
        except Exception:
            return web.json_response({"ok": False, "error": "whisper_request_failed"}, status=502)

        try:
            data = await resp.json()
        except Exception:
            txt = await resp.text()
            return web.json_response(
                {"ok": False, "error": "bad_response", "status": resp.status, "body": txt[:500]},
                status=502,
            )

        if resp.status >= 300:
            return web.json_response({"ok": False, "error": "whisper_error", "status": resp.status, "details": data}, status=502)

        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str):
            text = ""
        return web.json_response({"ok": True, "text": text.strip()})


class ClawdbotAvatarPngView(HomeAssistantView):
    """Serve the active avatar PNG."""

    url = "/api/clawdbot/avatar.png"
    name = "api:clawdbot:avatar_png"
    requires_auth = False

    async def get(self, request):
        from aiohttp import web
        import base64

        cfg = request.app["hass"].data.get(DOMAIN, {})
        avatar = cfg.get("avatar")
        if not isinstance(avatar, dict):
            raise web.HTTPNotFound()

        # Back-compat: older builds stored it at png_b64.
        png_b64 = avatar.get("active_png_b64") or avatar.get("png_b64")
        if not isinstance(png_b64, str) or not png_b64:
            raise web.HTTPNotFound()

        b64 = png_b64
        if b64.startswith("data:"):
            try:
                b64 = b64.split(",", 1)[1]
            except Exception:
                raise web.HTTPNotFound()

        try:
            raw = base64.b64decode(b64)
        except Exception:
            raise web.HTTPNotFound()

        return web.Response(
            body=raw,
            content_type="image/png",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )


class ClawdbotAvatarPreviewPngView(HomeAssistantView):
    """Serve a request_id-scoped preview PNG (not necessarily active)."""

    url = "/api/clawdbot/avatar_preview.png"
    name = "api:clawdbot:avatar_preview_png"
    requires_auth = False

    async def get(self, request):
        from aiohttp import web
        import base64

        hass = request.app["hass"]
        cfg = hass.data.get(DOMAIN, {})
        avatar = cfg.get("avatar")
        if not isinstance(avatar, dict):
            raise web.HTTPNotFound()

        q = request.query
        request_id = q.get("request_id") if q else None
        if not isinstance(request_id, str) or not request_id.strip():
            raise web.HTTPBadRequest()
        request_id = request_id.strip()

        previews = avatar.get("previews")
        if not isinstance(previews, dict):
            raise web.HTTPNotFound()
        item = previews.get(request_id)
        if not isinstance(item, dict):
            raise web.HTTPNotFound()
        png_b64 = item.get("png_b64")
        if not isinstance(png_b64, str) or not png_b64:
            raise web.HTTPNotFound()

        b64 = png_b64
        if b64.startswith("data:"):
            try:
                b64 = b64.split(",", 1)[1]
            except Exception:
                raise web.HTTPNotFound()

        try:
            raw = base64.b64decode(b64)
        except Exception:
            raise web.HTTPNotFound()

        return web.Response(
            body=raw,
            content_type="image/png",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )


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
        store: Store = cfg.get("chat_store")
        if store is not None:
            items = await store.async_load() or []
        else:
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
        # Optional incremental paging
        after_ts = request.query.get("after_ts") or request.query.get("since_ts")
        before_id = request.query.get("before_id")

        # Always sort by timestamp ascending (oldest->newest) for deterministic paging.
        def _ts(it: dict) -> str:
            try:
                return str(it.get("ts") or "")
            except Exception:
                return ""

        filtered.sort(key=_ts)

        if after_ts:
            # Return items strictly newer than after_ts
            candidates = [it for it in filtered if str(it.get("ts") or "") > str(after_ts)]
            # Cap to limit (newest-last)
            page = candidates[:limit]
            has_older = False
            return web.json_response({"ok": True, "items": page, "has_older": has_older})

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


class ClawdbotSessionsApiView(HomeAssistantView):
    """Authenticated API for listing OpenClaw sessions (for chat session switcher)."""

    url = "/api/clawdbot/sessions"
    name = "api:clawdbot:sessions"
    requires_auth = True

    async def get(self, request):
        from aiohttp import web

        hass = request.app["hass"]
        session, gateway_origin, token, session_key, err = _runtime_gateway_parts_http(hass)
        if err:
            return web.json_response({"ok": False, "error": err}, status=400)

        limit = 50
        try:
            limit = int(request.query.get("limit", 50))
        except Exception:
            limit = 50
        if limit < 1:
            limit = 1
        if limit > 200:
            limit = 200

        payload = {"tool": "sessions_list", "args": {"limit": limit, "messageLimit": 1}}
        res = await _gw_post(session, gateway_origin + "/tools/invoke", token, payload)
        return web.json_response({"ok": True, "result": res})


class ClawdbotSessionsHistoryApiView(HomeAssistantView):
    """Authenticated API for polling OpenClaw session history (sanitized)."""

    url = "/api/clawdbot/sessions_history"
    name = "api:clawdbot:sessions_history"
    requires_auth = True

    async def get(self, request):
        from aiohttp import web

        hass = request.app["hass"]
        session, gateway_origin, token, session_key, err = _runtime_gateway_parts_http(hass)
        if err:
            return web.json_response({"ok": False, "error": err}, status=400)

        session_key = request.query.get("session_key")
        if not session_key:
            return web.json_response({"ok": False, "error": "session_key required"}, status=400)

        limit = 20
        try:
            limit = int(request.query.get("limit", 20))
        except Exception:
            limit = 20
        if limit < 1:
            limit = 1
        if limit > 100:
            limit = 100

        payload = {"tool": "sessions_history", "args": {"sessionKey": session_key, "limit": limit}}
        res = await _gw_post(session, gateway_origin + "/tools/invoke", token, payload)

        raw = res
        # Some gateway responses double-wrap result/result.
        for _ in range(3):
            if isinstance(raw, dict) and "result" in raw and isinstance(raw.get("result"), (dict, list)):
                raw = raw.get("result")
            else:
                break

        if request.query.get("debug") == "1":
            try:
                _LOGGER.info("sessions_history debug: top-level type=%s keys=%s", type(raw), list(raw.keys()) if isinstance(raw, dict) else None)
            except Exception:
                pass

        # OpenClaw /tools/invoke typically returns {content:[...], details:{...}}
        if isinstance(raw, dict) and isinstance(raw.get("details"), dict):
            details = raw.get("details")
            if isinstance(details.get("messages"), list):
                raw = details

        # Sometimes the JSON is embedded in content[0].text
        if isinstance(raw, dict) and not isinstance(raw.get("messages"), list) and isinstance(raw.get("content"), list):
            try:
                import json

                txt = raw.get("content")[0].get("text") if raw.get("content") else None
                if isinstance(txt, str) and txt.strip().startswith("{"):
                    parsed = json.loads(txt)
                    if isinstance(parsed, dict):
                        raw = parsed
            except Exception:
                pass

        messages = None
        if isinstance(raw, list):
            messages = raw
        elif isinstance(raw, dict):
            for key in ("items", "messages", "history", "data", "result"):
                value = raw.get(key)
                if isinstance(value, list):
                    messages = value
                    break
        if messages is None:
            messages = []

        items = []
        now_ms = int(time.time() * 1000)
        for msg in messages:
            if not isinstance(msg, dict):
                continue

            role_raw = msg.get("role") or msg.get("author")
            if role_raw == "assistant":
                role = "agent"
            elif role_raw == "user":
                role = "user"
            else:
                continue

            content = msg.get("content")
            parts = []
            signature = None

            def _pull_text(part_obj):
                nonlocal signature
                if not isinstance(part_obj, dict):
                    return
                if part_obj.get("type") != "text":
                    return
                txt = part_obj.get("text")
                if txt is None:
                    txt = part_obj.get("content")
                if txt is None:
                    txt = ""
                parts.append(str(txt))
                if signature is None:
                    sig = part_obj.get("textSignature")
                    if sig:
                        signature = str(sig)

            if isinstance(content, list):
                for part in content:
                    _pull_text(part)
            elif isinstance(content, dict):
                if isinstance(content.get("parts"), list):
                    for part in content.get("parts"):
                        _pull_text(part)
                else:
                    _pull_text(content)
            elif isinstance(content, str):
                parts.append(content)

            text = "".join(parts)
            if not text.strip():
                continue

            ts_ms = None
            for key in ("timestamp", "ts", "time", "createdAt", "created_at"):
                if key in msg:
                    ts_ms = msg.get(key)
                    break
            try:
                ts_ms = int(ts_ms) if ts_ms is not None else None
            except Exception:
                ts_ms = None
            if ts_ms is None:
                ts_ms = now_ms

            item_id = signature or hashlib.sha256(
                f"{session_key}{ts_ms}{role}{text}".encode("utf-8")
            ).hexdigest()

            items.append(
                {
                    "id": item_id,
                    "ts": _iso_from_ms(ts_ms),
                    "role": role,
                    "session_key": session_key,
                    "text": text,
                }
            )

        return web.json_response({"ok": True, "items": items})


class ClawdbotSessionStatusApiView(HomeAssistantView):
    """Authenticated API for best-effort token usage display."""

    url = "/api/clawdbot/session_status"
    name = "api:clawdbot:session_status"
    requires_auth = True

    async def get(self, request):
        from aiohttp import web

        hass = request.app["hass"]
        session, gateway_origin, token, session_key, err = _runtime_gateway_parts_http(hass)
        if err:
            return web.json_response({"ok": False, "error": err}, status=400)

        session_key = request.query.get("session_key")
        if not session_key:
            return web.json_response({"ok": False, "error": "session_key required"}, status=400)

        payload = {"tool": "session_status", "args": {"sessionKey": session_key}}
        res = await _gw_post(session, gateway_origin + "/tools/invoke", token, payload)

        # Sanitize heavily: never return raw status cards (may include auth snippets).
        raw = res
        if isinstance(raw, dict) and "result" in raw:
            raw = raw.get("result")
        if isinstance(raw, dict) and "result" in raw:
            raw = raw.get("result")

        usage = None
        busy = None
        if isinstance(raw, dict):
            usage = raw.get("usage") or raw.get("Usage")
            busy = raw.get("busy") if "busy" in raw else raw.get("in_flight")
        if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
            d = raw.get("data")
            usage = usage or d.get("usage")
            busy = busy if busy is not None else d.get("busy")

        # Return ONLY numeric token counters + busy flag. Never include raw status text.
        safe_usage = None
        if isinstance(usage, dict):
            safe_usage = {}
            for k in ("totalTokens", "input", "output", "cacheRead", "cacheWrite"):
                v = usage.get(k)
                if isinstance(v, (int, float)):
                    safe_usage[k] = v

        out = {"ok": True, "session_key": session_key, "busy": bool(busy) if busy is not None else None, "usage": safe_usage}

        # Belt-and-suspenders: scrub any accidental token-like strings.
        def _scrub(obj):
            if isinstance(obj, dict):
                return {k: _scrub(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_scrub(v) for v in obj]
            if isinstance(obj, str) and "sk-" in obj:
                return "[REDACTED]"
            return obj

        return web.json_response(_scrub(out))


class ClawdbotSessionsSendApiView(HomeAssistantView):
    """Authenticated API for sending chat messages into an OpenClaw session."""

    url = "/api/clawdbot/sessions_send"
    name = "api:clawdbot:sessions_send"
    requires_auth = True

    async def post(self, request):
        from aiohttp import web

        hass = request.app["hass"]
        session, gateway_origin, token, session_key, err = _runtime_gateway_parts_http(hass)
        if err:
            return web.json_response({"ok": False, "error": err}, status=400)

        try:
            data = await request.json()
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}

        rt = _runtime(hass)
        session_key = data.get("session_key") or data.get("sessionKey") or rt.get("session_key") or DEFAULT_SESSION_KEY
        message = data.get("message")
        if not isinstance(message, str) or not message.strip():
            return web.json_response({"ok": False, "error": "message is required"}, status=400)

        payload = {"tool": "sessions_send", "args": {"sessionKey": str(session_key), "message": message}}
        res = await _gw_post(session, gateway_origin + "/tools/invoke", token, payload)
        return web.json_response({"ok": True, "result": res})


class ClawdbotSessionsSpawnApiView(HomeAssistantView):
    """Authenticated API for spawning a new OpenClaw session (best-effort)."""

    url = "/api/clawdbot/sessions_spawn"
    name = "api:clawdbot:sessions_spawn"
    requires_auth = True

    async def post(self, request):
        from aiohttp import web

        hass = request.app["hass"]
        session, gateway_origin, token, session_key, err = _runtime_gateway_parts_http(hass)
        if err:
            return web.json_response({"ok": False, "error": err}, status=400)

        try:
            data = await request.json()
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        label = data.get("label")

        payload = {"tool": "sessions_spawn", "args": {"task": "(new chat session)", "label": label or None, "cleanup": "keep"}}
        res = await _gw_post(session, gateway_origin + "/tools/invoke", token, payload)
        return web.json_response({"ok": True, "result": res})



    url = "/api/clawdbot/sessions_send"
    name = "api:clawdbot:sessions_send"
    requires_auth = True

    async def post(self, request):
        from aiohttp import web

        hass = request.app["hass"]
        session, gateway_origin, token, session_key, err = _runtime_gateway_parts_http(hass)
        if err:
            return web.json_response({"ok": False, "error": err}, status=400)

        try:
            data = await request.json()
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        session_key = data.get("session_key") or data.get("sessionKey")
        message = data.get("message")
        if not session_key or not message:
            return web.json_response({"ok": False, "error": "session_key and message required"}, status=400)

        payload = {"tool": "sessions_send", "args": {"sessionKey": str(session_key), "message": str(message)}}
        res = await _gw_post(session, gateway_origin + "/tools/invoke", token, payload)
        return web.json_response({"ok": True, "result": res})



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
    conf = config.get(DOMAIN, {}) or {}
    # For MVP: always serve panel content from HA itself.
    # This avoids OpenClaw Control UI auth/device-identity and makes the iframe same-origin.
    panel_url = PANEL_PATH

    title = conf.get("title", DEFAULT_TITLE)
    icon = conf.get("icon", DEFAULT_ICON)

    hass.data.setdefault(DOMAIN, {})
    # Keep the YAML conf around so we can recompute effective config after resetting overrides.
    hass.data[DOMAIN]["yaml_conf"] = conf

    # Load Store-backed connection overrides.
    overrides_store = Store(hass, OVERRIDES_STORE_VERSION, OVERRIDES_STORE_KEY)
    overrides = await overrides_store.async_load() or {}
    if not isinstance(overrides, dict):
        overrides = {}

    def _pick(key: str, yaml_key: str | None = None, default=None):
        if key in overrides:
            return overrides.get(key)
        if yaml_key is not None and yaml_key in conf:
            return conf.get(yaml_key)
        return default

    gateway_url = _pick("gateway_url", CONF_GATEWAY_URL, None)
    token = _pick("token", CONF_TOKEN, None)
    session_key = _pick("session_key", CONF_SESSION_KEY, DEFAULT_SESSION_KEY)

    gateway_origin = None
    if isinstance(gateway_url, str) and gateway_url.strip():
        gateway_origin = _derive_gateway_origin(gateway_url).rstrip("/")

    # Use Home Assistant's configured aiohttp session factory.
    from homeassistant.helpers.aiohttp_client import async_create_clientsession

    session = async_create_clientsession(hass)

    runtime = {
        "gateway_url": gateway_url,
        "gateway_origin": gateway_origin,
        "token": token,
        "has_token": bool(token),
        "session_key": session_key,
        "session": session,
        "overrides_store": overrides_store,
        "overrides": overrides,
        # Chat ingest guardrails
        "chat_dedupe": {},  # {fingerprint: ts_epoch}
        "chat_last_agent_text": {},  # {session_key: {"text": str, "ts": epoch}}
    }
    hass.data[DOMAIN]["runtime"] = runtime

    # Store sanitized config for the panel (never expose the token).

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
            "store": store,
            "mapping": mapping,
            "house_store": house_store,
            "house_memory": house_memory,
        }
    )

    # Load derived-sensor settings (Store-backed enablement)
    derived_store = Store(hass, DERIVED_STORE_VERSION, DERIVED_STORE_KEY)
    derived_cfg = await derived_store.async_load() or {}
    if not isinstance(derived_cfg, dict):
        derived_cfg = {}
    derived_enabled = bool(derived_cfg.get("enabled"))

    runtime.update(
        {
            "derived_store": derived_store,
            "derived_cfg": derived_cfg,
            "derived_enabled": derived_enabled,
            "derived_task": None,
            "derived_state": {},
            "derived_last_update": None,
        }
    )

    # Agent0 history ring-buffer (no recorder dependency)
    agent0_hist_store = Store(hass, AGENT0_HIST_STORE_VERSION, AGENT0_HIST_STORE_KEY)
    agent0_hist_blob = await agent0_hist_store.async_load() or {}
    if not isinstance(agent0_hist_blob, dict):
        agent0_hist_blob = {}
    agent0_hist = agent0_hist_blob.get("series") if isinstance(agent0_hist_blob.get("series"), dict) else {}
    if not isinstance(agent0_hist, dict):
        agent0_hist = {}

    runtime.update(
        {
            "agent0_hist_store": agent0_hist_store,
            "agent0_hist": agent0_hist,  # {entity_id: [[ts_epoch, val], ...]}
            "agent0_hist_last_persist": None,
            "agent0_hist_sampler_task": None,
        }
    )

    # Load chat history
    chat_store = Store(hass, CHAT_STORE_VERSION, CHAT_STORE_KEY)
    chat_history = await chat_store.async_load() or []
    if not isinstance(chat_history, list):
        chat_history = []

    # Load chat sessions list (HA-side) so UI can create/switch sessions reliably.
    chat_sessions_store = Store(hass, CHAT_SESSIONS_STORE_VERSION, CHAT_SESSIONS_STORE_KEY)
    chat_sessions = await chat_sessions_store.async_load() or {}
    if not isinstance(chat_sessions, dict):
        chat_sessions = {}
    items = chat_sessions.get("items")
    if not isinstance(items, list):
        items = []
    # Always include default session
    if not any(isinstance(it, dict) and it.get("key") == DEFAULT_SESSION_KEY for it in items):
        items.insert(0, {"key": DEFAULT_SESSION_KEY, "label": "Main"})
    chat_sessions["items"] = items
    await chat_sessions_store.async_save(chat_sessions)

    # Load theme settings (Store-backed)
    theme_store = Store(hass, THEME_STORE_VERSION, THEME_STORE_KEY)
    theme_cfg = await theme_store.async_load() or {}
    if not isinstance(theme_cfg, dict):
        theme_cfg = {}
    theme_preset = theme_cfg.get("preset") or "nebula"
    theme_auto = bool(theme_cfg.get("auto"))
    # Custom themes: dict key->theme object
    theme_custom = theme_cfg.get("themes")
    if not isinstance(theme_custom, dict):
        theme_custom = {}

    # Dynamic Setup options registry (Store-backed)
    setup_options_store = Store(hass, SETUP_OPTIONS_STORE_VERSION, SETUP_OPTIONS_STORE_KEY)
    setup_registry = await setup_options_store.async_load() or {}
    if not isinstance(setup_registry, dict):
        setup_registry = {}
    setup_options = setup_registry.get("options")
    if not isinstance(setup_options, dict):
        setup_options = {}
        setup_registry["options"] = setup_options

    # Journal store (append-only, capped)
    journal_store = Store(hass, JOURNAL_STORE_VERSION, JOURNAL_STORE_KEY)
    journal_items = await journal_store.async_load() or []

    # Agent state webhook id (for Agent 0 cross-host push, no token required)
    agent_state_webhook_store = Store(
        hass, AGENT_STATE_WEBHOOK_STORE_VERSION, AGENT_STATE_WEBHOOK_STORE_KEY
    )
    agent_state_webhook = await agent_state_webhook_store.async_load() or {}
    if not isinstance(agent_state_webhook, dict):
        agent_state_webhook = {}

    avatar_webhook_store = Store(hass, AVATAR_WEBHOOK_STORE_VERSION, AVATAR_WEBHOOK_STORE_KEY)
    avatar_webhook = await avatar_webhook_store.async_load() or {}
    if not isinstance(avatar_webhook, dict):
        avatar_webhook = {}
    if not isinstance(journal_items, list):
        journal_items = []

    # Agent profile store (mood + description)
    agent_profile_store = Store(hass, AGENT_PROFILE_STORE_VERSION, AGENT_PROFILE_STORE_KEY)
    agent_profile = await agent_profile_store.async_load() or {}
    if not isinstance(agent_profile, dict):
        agent_profile = {}

    avatar_store = Store(hass, AVATAR_STORE_VERSION, AVATAR_STORE_KEY)
    avatar = await avatar_store.async_load() or {}
    if not isinstance(avatar, dict):
        avatar = {}

    hass.data[DOMAIN].update(
        {
            "chat_store": chat_store,
            "chat_history": chat_history[-500:],
            "chat_sessions_store": chat_sessions_store,
            "chat_sessions": chat_sessions,
            "theme_store": theme_store,
            "theme_cfg": theme_cfg,
            "theme": {"preset": theme_preset, "auto": theme_auto, "themes": theme_custom},
            "setup_options_store": setup_options_store,
            "setup_registry": setup_registry,
            "setup_options": setup_options,
            "journal_store": journal_store,
            "journal": journal_items[-200:],
            "agent_profile_store": agent_profile_store,
            "agent_profile": agent_profile,
            "avatar_store": avatar_store,
            "avatar": avatar,
            "agent_state_webhook_store": agent_state_webhook_store,
            "agent_state_webhook": agent_state_webhook,
            "avatar_webhook_store": avatar_webhook_store,
            "avatar_webhook": avatar_webhook,
        }
    )

    # HTTP view (served by HA)
    try:
        hass.http.register_view(ClawdbotPanelView)
        hass.http.register_view(ClawdbotPanelJsView)
        hass.http.register_view(ClawdbotMappingApiView)
        hass.http.register_view(ClawdbotPanelSelfTestApiView)
        hass.http.register_view(ClawdbotSttWhisperApiView)
        hass.http.register_view(ClawdbotAvatarPngView)
        hass.http.register_view(ClawdbotAvatarPreviewPngView)
        hass.http.register_view(ClawdbotHouseMemoryApiView)
        hass.http.register_view(ClawdbotChatHistoryApiView)
        hass.http.register_view(ClawdbotSessionsApiView)
        hass.http.register_view(ClawdbotSessionsHistoryApiView)
        hass.http.register_view(ClawdbotSessionStatusApiView)
        hass.http.register_view(ClawdbotSessionsSendApiView)
        hass.http.register_view(ClawdbotSessionsSpawnApiView)
        _LOGGER.info("Registered Clawdbot panel view → %s", PANEL_PATH)
        _LOGGER.info("Registered Clawdbot mapping API → %s", ClawdbotMappingApiView.url)
    except Exception:
        _LOGGER.exception("Failed to register Clawdbot HTTP views")

    # Register agent state webhook handler (cross-host push without token)
    try:
        from homeassistant.components import webhook
        from aiohttp.web import Response

        store: Store = hass.data[DOMAIN].get("agent_state_webhook_store")
        data = hass.data[DOMAIN].get("agent_state_webhook")
        if store is not None and isinstance(data, dict):
            webhook_id = data.get("webhook_id")
            if not isinstance(webhook_id, str) or not webhook_id:
                webhook_id = webhook.async_generate_id()
                data = {"webhook_id": webhook_id}
                await store.async_save(data)
                hass.data[DOMAIN]["agent_state_webhook"] = data

            async def _handle_agent_state_webhook(hass, webhook_id, request):
                try:
                    payload = await request.json()
                except Exception:
                    return Response(status=200)
                if not isinstance(payload, dict):
                    return Response(status=200)

                call_data = {
                    "mood": payload.get("mood"),
                    "description": payload.get("description"),
                    "journal": payload.get("journal"),
                    "source": payload.get("source") or "agent0",
                }

                try:
                    class _Call:
                        __slots__ = ("data",)

                        def __init__(self, data):
                            self.data = data

                    await handle_agent_state_set(_Call(call_data))
                except Exception:
                    return Response(status=200)
                return Response(status=200)

            webhook.async_register(
                hass,
                DOMAIN,
                "agent_state_push",
                webhook_id,
                _handle_agent_state_webhook,
                local_only=False,
                allowed_methods=("POST",),
            )
            _LOGGER.info("Registered agent state webhook → /api/webhook/%s", webhook_id)
    except Exception:
        _LOGGER.exception("Failed to register agent state webhook")

    # Register avatar webhook handler (Agent0 can POST png_b64 without tokens)
    try:
        from homeassistant.components import webhook
        from aiohttp.web import Response

        store: Store = hass.data[DOMAIN].get("avatar_webhook_store")
        data = hass.data[DOMAIN].get("avatar_webhook")
        if store is not None and isinstance(data, dict):
            webhook_id = data.get("webhook_id")
            if not isinstance(webhook_id, str) or not webhook_id:
                webhook_id = webhook.async_generate_id()
                data = {"webhook_id": webhook_id}
                await store.async_save(data)
                hass.data[DOMAIN]["avatar_webhook"] = data

            async def _handle_avatar_webhook(hass, webhook_id, request):
                """Accept {agent_id, png_b64} and store via avatar_set_b64.

                Always returns 200 so callers don't retry indefinitely, but includes JSON ok/error for debugging.
                """
                from aiohttp import web

                try:
                    payload = await request.json()
                except Exception:
                    return web.json_response({"ok": False, "error": "invalid_json"}, status=200)
                if not isinstance(payload, dict):
                    return web.json_response({"ok": False, "error": "invalid_payload"}, status=200)

                agent_id = payload.get("agent_id") or "agent0"
                png_b64 = payload.get("png_b64")
                approx_len = len(png_b64) if isinstance(png_b64, str) else 0

                call_data = {
                    "agent_id": agent_id,
                    "request_id": payload.get("request_id"),
                    "png_b64": png_b64,
                }

                try:
                    class _Call:
                        __slots__ = ("data",)

                        def __init__(self, data):
                            self.data = data

                    await handle_avatar_set_b64(_Call(call_data))
                except Exception as e:
                    _LOGGER.warning(
                        "avatar webhook: failed to store avatar (agent_id=%s b64_len=%s): %s",
                        agent_id,
                        approx_len,
                        str(e)[:200],
                    )
                    return web.json_response({"ok": False, "error": str(e)[:200]}, status=200)

                _LOGGER.info(
                    "avatar webhook: stored avatar (agent_id=%s b64_len=%s)", agent_id, approx_len
                )
                return web.json_response({"ok": True}, status=200)

            webhook.async_register(
                hass,
                DOMAIN,
                "avatar_push",
                webhook_id,
                _handle_avatar_webhook,
                local_only=False,
                allowed_methods=("POST",),
            )
            _LOGGER.info("Registered avatar webhook → /api/webhook/%s", webhook_id)
    except Exception:
        _LOGGER.exception("Failed to register avatar webhook")

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


    async def _notify(title: str, message: str) -> None:
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {"title": title, "message": message[:4000]},
            blocking=False,
        )

    # Services

    async def _apply_runtime_from_overrides(hass) -> dict[str, Any]:
        """Recompute effective connection config, update runtime, and swap aiohttp session."""
        cfg = hass.data.get(DOMAIN, {})
        rt = cfg.get("runtime")
        if not isinstance(rt, dict):
            raise HomeAssistantError("runtime not initialized")

        yaml_conf = cfg.get("yaml_conf", {}) or {}
        store: Store = rt.get("overrides_store")
        if store is None:
            raise HomeAssistantError("overrides store not initialized")

        overrides = await store.async_load() or {}
        if not isinstance(overrides, dict):
            overrides = {}

        def pick(key: str, yaml_key: str, default=None):
            if key in overrides:
                return overrides.get(key)
            return yaml_conf.get(yaml_key, default)

        gateway_url = pick("gateway_url", CONF_GATEWAY_URL, None)
        token_val = pick("token", CONF_TOKEN, None)
        session_key_val = pick("session_key", CONF_SESSION_KEY, DEFAULT_SESSION_KEY)

        gateway_origin = None
        if isinstance(gateway_url, str) and gateway_url.strip():
            gateway_origin = _derive_gateway_origin(gateway_url).rstrip("/")

        # Swap session (safe close; never block).
        old = rt.get("session")
        if old is not None:
            try:
                await old.close()
            except Exception:
                _LOGGER.warning("Failed to close old aiohttp session", exc_info=True)

        from homeassistant.helpers.aiohttp_client import async_create_clientsession
        rt["session"] = async_create_clientsession(hass)

        rt.update(
            {
                "gateway_url": gateway_url,
                "gateway_origin": gateway_origin,
                "token": token_val,
                "has_token": bool(token_val),
                "session_key": session_key_val,
                "overrides": overrides,
            }
        )
        return {
            "ok": True,
            "gateway_url": gateway_url,
            "has_token": bool(token_val),
            "session_key": session_key_val,
        }

    async def handle_set_connection_overrides(call):
        hass = call.hass
        cfg = hass.data.get(DOMAIN, {})
        rt = cfg.get("runtime")
        if not isinstance(rt, dict):
            raise HomeAssistantError("runtime not initialized")
        store: Store = rt.get("overrides_store")
        if store is None:
            raise HomeAssistantError("overrides store not initialized")

        yaml_conf = cfg.get("yaml_conf", {}) or {}
        overrides = await store.async_load() or {}
        if not isinstance(overrides, dict):
            overrides = {}

        gw_in = call.data.get("gateway_url")
        sk_in = call.data.get("session_key")
        token_in = call.data.get("token")

        # gateway_url + session_key are prefilled in UI; blank => leave unchanged.
        if isinstance(gw_in, str):
            gw_in = gw_in.strip()
            if gw_in:
                if gw_in == (yaml_conf.get(CONF_GATEWAY_URL) or ""):
                    overrides.pop("gateway_url", None)
                else:
                    overrides["gateway_url"] = gw_in
        if isinstance(sk_in, str):
            sk_in = sk_in.strip()
            if sk_in:
                if sk_in == (yaml_conf.get(CONF_SESSION_KEY) or DEFAULT_SESSION_KEY):
                    overrides.pop("session_key", None)
                else:
                    overrides["session_key"] = sk_in

        # token is never prefilled; blank => keep current token override/yaml.
        if isinstance(token_in, str):
            token_in = token_in.strip()
            if token_in:
                if token_in == (yaml_conf.get(CONF_TOKEN) or ""):
                    overrides.pop("token", None)
                else:
                    overrides["token"] = token_in

        await store.async_save(overrides)
        return await _apply_runtime_from_overrides(hass)

    async def handle_reset_connection_overrides(call):
        hass = call.hass
        rt = _runtime(hass)
        store: Store = rt.get("overrides_store")
        if store is None:
            raise HomeAssistantError("overrides store not initialized")
        await store.async_save({})
        return await _apply_runtime_from_overrides(hass)

    async def handle_send_chat(call):
        hass = call.hass
        session, gateway_origin, token, session_key = _runtime_gateway_parts(hass)

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
        hass = call.hass
        session, gateway_origin, token, _default_session_key = _runtime_gateway_parts(hass)

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
        rt = _runtime(hass)
        session = call.data.get("session_key") or rt.get("session_key") or DEFAULT_SESSION_KEY
        provided_id = call.data.get("id")
        provided_ts = call.data.get("ts")

        if role not in {"user", "agent"}:
            raise RuntimeError("role must be one of: user, agent")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("text is required")
        if not isinstance(session, str) or not session:
            session = DEFAULT_SESSION_KEY

        item_id = str(provided_id) if provided_id else str(time.time_ns())
        item_ts = str(provided_ts).strip() if provided_ts is not None else ""
        if not item_ts:
            item_ts = dt.datetime.now(tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")

        # Guardrails: drop internal plumbing lines + role-flip echoes.
        plumbing_markers = (
            "Agent-to-agent announce step.",
            "agent-to-agent announce step.",
        )
        import re as _re
        # Drop internal plumbing/control lines from user-visible history.
        if any(m in text for m in plumbing_markers):
            return
        if _re.search(r"\bANNOUNCE_\w+\b", text):
            return
        if _re.search(r"\b(HEARTBEAT_OK|NO_REPLY)\b", text):
            return
        if _re.search(r"agent-to-agent announce", text, flags=_re.I):
            return

        try:
            import datetime as _dt

            rt = _runtime(hass)
            last = (rt.get("chat_last_agent_text") or {}).get(session) if isinstance(rt.get("chat_last_agent_text"), dict) else None
            if role == "user" and isinstance(last, dict) and last.get("text") == text:
                # If a user message exactly matches the last agent message within 10s, it's almost certainly an echo-loop.
                try:
                    last_ts = float(last.get("ts") or 0)
                except Exception:
                    last_ts = 0
                now_ts = __import__("time").time()
                if last_ts and (now_ts - last_ts) <= 10:
                    return
        except Exception:
            pass

        # Fingerprint-based dedupe (cross-source) at store-write time
        try:
            import hashlib as _hashlib
            import re as _re

            # Normalize whitespace to make dedupe resilient.
            norm = _re.sub(r"\s+", " ", text).strip()

            # Bucket based on item_ts (not wall clock) to avoid collapsing many distinct messages.
            # item_ts is ISO; we fall back to wall clock if parsing fails.
            fp_bucket = None
            try:
                from homeassistant.util import dt as _dt_util

                dt_obj = _dt_util.parse_datetime(item_ts.replace("Z", "+00:00"))
                if dt_obj is not None:
                    fp_bucket = int(dt_obj.timestamp() // 2)
            except Exception:
                fp_bucket = None
            if fp_bucket is None:
                fp_bucket = int(__import__("time").time() // 2)

            fp = _hashlib.sha256(f"{session}|{role}|{norm}|{fp_bucket}".encode("utf-8")).hexdigest()
        except Exception:
            fp = None

        item = {
            "id": item_id,
            "ts": item_ts,
            "role": role,
            "session_key": session,
            "text": text,
            "source": "panel",
            "direction": "inbound",
            "fingerprint": fp,
        }

        items = cfg.get("chat_history", []) or []
        if not isinstance(items, list):
            items = []
        for it in items:
            if not isinstance(it, dict):
                continue
            if it.get("id") == item_id:
                return
            # fingerprint dedupe (prevents duplicates when both chat_append and other paths write same message)
            if fp and it.get("fingerprint") == fp:
                return
        items.append(item)
        if len(items) > 500:
            items = items[-500:]

        await store.async_save(items)
        cfg["chat_history"] = items

        # Track last agent text to detect role-flip echoes.
        try:
            rt = _runtime(hass)
            if role == "agent":
                d = rt.get("chat_last_agent_text")
                if not isinstance(d, dict):
                    d = {}
                    rt["chat_last_agent_text"] = d
                d[session] = {"text": text, "ts": __import__("time").time()}
        except Exception:
            pass

    async def handle_chat_send(call):
        """Send a user message into an OpenClaw session (server-side).

        IMPORTANT: Do NOT append the user message to the chat store here. The panel already
        calls `chat_append` before `chat_send`, and double-writing was causing duplicates.
        """
        hass = call.hass
        session, gateway_origin, token, default_session_key = _runtime_gateway_parts(hass)

        message = call.data.get("message")
        if not isinstance(message, str) or not message.strip():
            raise RuntimeError("message is required")

        session_key_local = call.data.get("session_key") or call.data.get("session") or default_session_key
        if not isinstance(session_key_local, str) or not session_key_local:
            session_key_local = DEFAULT_SESSION_KEY

        _LOGGER.info(
            "chat_send -> gateway sessions_send (session=%s, len=%s)",
            session_key_local,
            len(message),
        )
        payload = {"tool": "sessions_send", "args": {"sessionKey": session_key_local, "message": message}}
        res = await _gw_post(session, gateway_origin + "/tools/invoke", token, payload)
        _LOGGER.debug("chat_send gateway response: %s", str(res)[:500])

    async def handle_sessions_list(call):
        hass = call.hass
        session, gateway_origin, token, _default_session_key = _runtime_gateway_parts(hass)
        limit = 50
        try:
            limit = int(call.data.get("limit", 50))
        except Exception:
            limit = 50
        if limit < 1:
            limit = 1
        if limit > 200:
            limit = 200
        payload = {"tool": "sessions_list", "args": {"limit": limit, "messageLimit": 1}}
        res = await _gw_post(session, gateway_origin + "/tools/invoke", token, payload)
        return {"result": res}

    async def handle_sessions_spawn(call):
        hass = call.hass
        session, gateway_origin, token, _default_session_key = _runtime_gateway_parts(hass)
        label = call.data.get("label")
        payload = {"tool": "sessions_spawn", "args": {"task": "(new chat session)", "label": label or None, "cleanup": "keep"}}
        res = await _gw_post(session, gateway_origin + "/tools/invoke", token, payload)
        return {"result": res}

    def _extract_session_key(obj):
        # Best-effort extraction across nested gateway result shapes.
        if obj is None:
            return None
        if isinstance(obj, str):
            return obj if obj.strip() else None
        if isinstance(obj, dict):
            for k in ("sessionKey", "session_key", "key", "childSessionKey", "child_session_key"):
                v = obj.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            for k in ("result", "response", "details", "data"):
                v = obj.get(k)
                got = _extract_session_key(v)
                if got:
                    return got
            # Also scan common nested lists
            for k in ("items", "sessions"):
                v = obj.get(k)
                got = _extract_session_key(v)
                if got:
                    return got
        if isinstance(obj, list):
            for it in obj:
                got = _extract_session_key(it)
                if got:
                    return got
        return None

    async def handle_chat_new_session(call):
        """Create a new chat session key and persist it in HA Store.

        This calls the gateway sessions_spawn and extracts the returned session key.
        """
        hass = call.hass
        label = call.data.get("label")

        # Spawn on gateway
        session, gateway_origin, token, _default_session_key = _runtime_gateway_parts(hass)
        payload = {"tool": "sessions_spawn", "args": {"task": "(new chat session)", "label": label or None, "cleanup": "keep"}}
        res = await _gw_post(session, gateway_origin + "/tools/invoke", token, payload)
        key = _extract_session_key(res)
        if not key:
            # Build a sanitized debug summary (never include token) and return it in supports_response.
            dbg_obj = {"_keys": []}
            try:
                import json as _json

                def _summ(obj, depth=0):
                    if depth > 2:
                        return "…"
                    if isinstance(obj, dict):
                        out = {"_keys": sorted(list(obj.keys()))[:30]}
                        # Keep a few interesting leaf fields (redacted to presence/len/type).
                        # Always emit these fields so we can tell if the code path is deployed.
                        v = obj.get("childSessionKey") if "childSessionKey" in obj else None
                        out["childSessionKey_present"] = bool(v)
                        out["childSessionKey_type"] = type(v).__name__
                        try:
                            out["childSessionKey_len"] = len(str(v)) if v is not None else 0
                        except Exception:
                            out["childSessionKey_len"] = None
                        if "runId" in obj:
                            v = obj.get("runId")
                            out["runId"] = str(v)[:80] if v is not None else None
                        if "status" in obj:
                            v = obj.get("status")
                            out["status"] = str(v)[:80] if v is not None else None

                        for k in ("ok", "error", "message", "status", "result", "details", "response"):
                            if k in obj:
                                out[k] = _summ(obj.get(k), depth + 1)
                        return out
                    if isinstance(obj, list):
                        return {"_list_len": len(obj)}
                    if isinstance(obj, (str, int, float, bool)) or obj is None:
                        s = str(obj)
                        return s[:300]
                    return str(type(obj))

                dbg_obj = _summ(res)
                _LOGGER.error("chat_new_session: gateway sessions_spawn returned no key; debug=%s", _json.dumps(dbg_obj)[:1200])
            except Exception:
                _LOGGER.error("chat_new_session: gateway sessions_spawn returned no key (failed to summarize)")

            return {"ok": False, "reason": "gateway did not return session key", "debug": dbg_obj}

        cfg = hass.data.get(DOMAIN, {})
        store: Store = cfg.get("chat_sessions_store")
        sessions = cfg.get("chat_sessions")
        if store is None or not isinstance(sessions, dict):
            raise HomeAssistantError("chat sessions store not initialized")

        items = sessions.get("items")
        if not isinstance(items, list):
            items = []
        if not any(isinstance(it, dict) and it.get("key") == key for it in items):
            items.append({"key": key, "label": label or None})
        sessions["items"] = items
        await store.async_save(sessions)
        cfg["chat_sessions"] = sessions
        return {"ok": True, "session_key": key, "items": items}

    async def handle_chat_list_sessions(call):
        cfg = hass.data.get(DOMAIN, {})
        sessions = cfg.get("chat_sessions")
        items = sessions.get("items") if isinstance(sessions, dict) else []
        if not isinstance(items, list):
            items = []
        return {"ok": True, "items": items}

    async def handle_session_status_get(call):
        hass = call.hass
        session, gateway_origin, token, default_session_key = _runtime_gateway_parts(hass)
        session_key = call.data.get("session_key") or default_session_key
        payload = {"tool": "session_status", "args": {"sessionKey": session_key}}
        res = await _gw_post(session, gateway_origin + "/tools/invoke", token, payload)
        return {"result": res}

    async def handle_chat_poll(call):
        """Poll gateway sessions_history and append new messages into the HA chat store.

        Guardrails: dedupe, ignore role-flip echoes, and filter internal plumbing text.
        """
        hass = call.hass
        session, gateway_origin, token, default_session_key = _runtime_gateway_parts(hass)

        cfg = hass.data.get(DOMAIN, {})
        store: Store = cfg.get("chat_store")
        if store is None:
            raise RuntimeError("chat history store not initialized")

        rt = _runtime(hass)

        session_key_local = call.data.get("session_key") or default_session_key
        if not isinstance(session_key_local, str) or not session_key_local:
            session_key_local = DEFAULT_SESSION_KEY

        limit = 50
        try:
            limit = int(call.data.get("limit", 50))
        except Exception:
            limit = 50
        if limit < 1:
            limit = 1
        if limit > 100:
            limit = 100

        payload = {"tool": "sessions_history", "args": {"sessionKey": session_key_local, "limit": limit}}
        res = await _gw_post(session, gateway_origin + "/tools/invoke", token, payload)
        _LOGGER.debug("chat_poll gateway raw response: %s", str(res)[:800])

        # Reuse the same parsing logic as the sessions_history API view (tail+diff).
        raw = res
        for _ in range(3):
            if isinstance(raw, dict) and "result" in raw and isinstance(raw.get("result"), (dict, list)):
                raw = raw.get("result")
            else:
                break
        if isinstance(raw, dict) and isinstance(raw.get("details"), dict):
            details = raw.get("details")
            if isinstance(details.get("messages"), list):
                raw = details
        if isinstance(raw, dict) and not isinstance(raw.get("messages"), list) and isinstance(raw.get("content"), list):
            try:
                import json

                txt = raw.get("content")[0].get("text") if raw.get("content") else None
                if isinstance(txt, str) and txt.strip().startswith("{"):
                    parsed = json.loads(txt)
                    if isinstance(parsed, dict):
                        raw = parsed
            except Exception:
                pass

        messages = None
        if isinstance(raw, list):
            messages = raw
        elif isinstance(raw, dict):
            for key in ("items", "messages", "history", "data", "result"):
                value = raw.get(key)
                if isinstance(value, list):
                    messages = value
                    break
        if messages is None:
            messages = []

        # Only append agent messages (assistant) to avoid user duplication.
        now_ms = int(time.time() * 1000)
        candidates = []
        seen_roles = {}
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role_raw = msg.get("role") or msg.get("author")
            if role_raw:
                seen_roles[str(role_raw)] = seen_roles.get(str(role_raw), 0) + 1
            if role_raw not in {"assistant", "agent"}:
                continue

            content = msg.get("content")
            parts = []
            signature = None

            def _pull_text(part_obj):
                nonlocal signature
                if not isinstance(part_obj, dict):
                    return
                if part_obj.get("type") != "text":
                    return
                txt = part_obj.get("text")
                if txt is None:
                    txt = part_obj.get("content")
                if txt is None:
                    txt = ""
                parts.append(str(txt))
                if signature is None:
                    sig = part_obj.get("textSignature")
                    if sig:
                        signature = str(sig)

            if isinstance(content, list):
                for part in content:
                    _pull_text(part)
            elif isinstance(content, dict):
                if isinstance(content.get("parts"), list):
                    for part in content.get("parts"):
                        _pull_text(part)
                else:
                    _pull_text(content)
            elif isinstance(content, str):
                parts.append(content)

            # Fallback: some gateway/tool outputs may provide text directly
            if not parts and isinstance(msg.get("text"), str):
                parts.append(msg.get("text"))

            text = "".join(parts)
            if not text.strip():
                continue

            # Filter internal control/meta lines that should never surface in HA chat UI.
            txt_norm = text.strip()
            import re as _re
            if _re.search(r"\bANNOUNCE_\w+\b", txt_norm):
                continue
            if _re.search(r"\b(HEARTBEAT_OK|NO_REPLY)\b", txt_norm):
                continue
            if _re.search(r"agent-to-agent announce", txt_norm, flags=_re.I):
                continue
            # Filter internal Pulse reflection outputs from appearing in the chat tab.
            if "PULSE_INTERNAL" in txt_norm or "BEGIN_JSON" in txt_norm:
                continue

            ts_ms = None
            for key in ("timestamp", "ts", "time", "createdAt", "created_at"):
                if key in msg:
                    ts_ms = msg.get(key)
                    break
            try:
                ts_ms = int(ts_ms) if ts_ms is not None else None
            except Exception:
                ts_ms = None
            if ts_ms is None:
                ts_ms = now_ms

            item_id = signature or hashlib.sha256(
                f"{session_key_local}{ts_ms}agent{text}".encode("utf-8")
            ).hexdigest()

            # Compute fingerprint for cross-source dedupe
            try:
                import hashlib as _hashlib
                fp_bucket = int((ts_ms / 1000) // 2)
                fp = _hashlib.sha256(f"{session_key_local}|agent|{text}|{fp_bucket}".encode("utf-8")).hexdigest()
            except Exception:
                fp = None

            candidates.append(
                {
                    "id": item_id,
                    "ts": _iso_from_ms(ts_ms),
                    "role": "agent",
                    "session_key": session_key_local,
                    "text": text,
                    "source": "gateway_poll",
                    "direction": "inbound",
                    "fingerprint": fp,
                }
            )

        # Always load from Store to avoid stale cfg cache / cross-task drift.
        current = await store.async_load() or []
        if not isinstance(current, list):
            current = []
        current = [it for it in current if isinstance(it, dict)]
        seen_ids = {it.get("id") for it in current if it.get("id")}

        # Dedupe guardrails (fingerprint TTL + track last agent text per session)
        import re as _re
        dedupe = rt.get("chat_dedupe")
        if not isinstance(dedupe, dict):
            dedupe = {}
            rt["chat_dedupe"] = dedupe
        last_agent_map = rt.get("chat_last_agent_text")
        if not isinstance(last_agent_map, dict):
            last_agent_map = {}
            rt["chat_last_agent_text"] = last_agent_map

        def _fingerprint(item: dict, bucket_s: int = 5) -> str:
            import hashlib as _hashlib
            import time as _time
            t = item.get("ts") or ""
            # bucket by now if parse fails
            try:
                # ts is iso; we just bucket using current time for simplicity
                b = int(_time.time() // bucket_s)
            except Exception:
                b = 0
            base = f"{item.get('session_key')}|{item.get('role')}|{item.get('text')}|{b}"
            return _hashlib.sha256(base.encode("utf-8")).hexdigest()

        def _dedupe_ok(fp: str, ttl_s: int = 60) -> bool:
            import time as _time
            now = _time.time()
            # cleanup lazily
            for k, v in list(dedupe.items()):
                try:
                    if now - float(v) > ttl_s:
                        dedupe.pop(k, None)
                except Exception:
                    dedupe.pop(k, None)
            if fp in dedupe:
                return False
            dedupe[fp] = now
            return True

        plumbing_re = _re.compile(r"agent-to-agent announce", _re.I)
        control_re = _re.compile(r"\b(HEARTBEAT_OK|NO_REPLY)\b|\bANNOUNCE_\w+\b", _re.I)

        store_len_before = len(current)
        appended = 0
        for it in candidates:
            if it["id"] in seen_ids:
                continue
            # Filter internal plumbing/control leaks
            if isinstance(it.get("text"), str):
                ttxt = it.get("text").strip()
                if plumbing_re.search(ttxt):
                    continue
                if control_re.search(ttxt):
                    continue

            # Use per-item fingerprint when present, else compute one.
            fp = it.get("fingerprint") or _fingerprint(it)
            if not _dedupe_ok(fp):
                continue

            current.append(it)
            seen_ids.add(it["id"])
            appended += 1
            # update last-agent tracker
            try:
                last_agent_map[it.get("session_key") or DEFAULT_SESSION_KEY] = {"text": it.get("text"), "ts": __import__("time").time()}
            except Exception:
                pass

        if appended:
            # Ensure stable ordering by timestamp (oldest->newest) before trimming.
            def _ts(it):
                try:
                    return str(it.get("ts") or "")
                except Exception:
                    return ""

            current.sort(key=_ts)
            if len(current) > 500:
                current = current[-500:]
            await store.async_save(current)
            cfg["chat_history"] = current
        else:
            # Keep cfg mirror warm even when no append occurs.
            cfg["chat_history"] = current[-500:]

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "chat_poll: fetched=%s roles=%s candidates=%s appended=%s store_len=%s->%s session=%s",
                len(messages),
                seen_roles,
                len(candidates),
                appended,
                store_len_before,
                len(current),
                session_key_local,
            )

        # Fire-and-forget service; caller can diff chat_history to infer changes.
        return

    async def handle_chat_history_delta(call):
        """Return chat history items (optionally since after_ts) from the HA Store.

        This is used by the iframe panel to avoid relying on parent.hass.callApi.
        Supports:
        - after_ts / since_ts: return items strictly newer than timestamp
        - before_id: return older items before a given id
        """
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

        rt = _runtime(hass)
        session_key = call.data.get("session_key") or rt.get("session_key") or DEFAULT_SESSION_KEY
        after_ts = call.data.get("after_ts") or call.data.get("since_ts")
        before_id = call.data.get("before_id")

        items = await store.async_load() or []
        if not isinstance(items, list):
            items = []
        items = [it for it in items if isinstance(it, dict)]
        if session_key:
            items = [it for it in items if it.get("session_key") == session_key]

        items.sort(key=lambda it: str(it.get("ts") or ""))

        if after_ts:
            newer = [it for it in items if str(it.get("ts") or "") > str(after_ts)]
            page = newer[:limit]
            return {"items": page, "has_older": False}

        if before_id:
            idx = None
            for i, it in enumerate(items):
                if it.get("id") == before_id:
                    idx = i
                    break
            older = items[:idx] if idx is not None else items
            page = older[-limit:] if len(older) > limit else older
            has_older = len(older) > len(page)
            return {"items": page, "has_older": has_older}

        # default: last N
        page = items[-limit:] if len(items) > limit else items
        has_older = len(items) > len(page)
        return {"items": page, "has_older": has_older}

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

        rt = _runtime(hass)
        session = call.data.get("session_key") or rt.get("session_key") or DEFAULT_SESSION_KEY
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
        hass = call.hass
        session, gateway_origin, token, session_key = _runtime_gateway_parts(hass)

        # Lightweight ping via listing sessions (no side effects)
        payload = {"tool": "sessions_list", "args": {"limit": 1}}
        import time

        t0 = time.monotonic()
        try:
            await _gw_post(session, gateway_origin + "/tools/invoke", token, payload)
        except Exception as e:
            # NOTE: Keep logs token-safe (never log/echo token).
            await _notify("Clawdbot: gateway_test", f"ERROR: {e}")
            raise

        latency_ms = int((time.monotonic() - t0) * 1000)
        return {
            "ok": True,
            "gateway_origin": gateway_origin,
            "session_key": session_key,
            "latency_ms": latency_ms,
        }

    async def handle_tools_invoke(call):
        hass = call.hass
        session, gateway_origin, token, _default_session_key = _runtime_gateway_parts(hass)
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

    async def handle_create_dummy_entities(call):
        """Create/refresh a handful of dummy entities for panel mapping QA.

        We intentionally create them as state-only entities via hass.states.async_set so this
        works without YAML edits or UI helper creation.
        """
        # Defaults
        soc = call.data.get("soc", 55)
        voltage = call.data.get("voltage", 52.4)
        solar_w = call.data.get("solar_w", 420)
        load_w = call.data.get("load_w", 380)
        connected = call.data.get("connected", True)

        def _set(eid: str, state, attrs: dict):
            hass.states.async_set(eid, str(state), attrs)

        _set(
            "sensor.clawdbot_test_battery_soc",
            soc,
            {
                "friendly_name": "Clawdbot Test Battery SOC",
                "unit_of_measurement": "%",
                "icon": "mdi:battery",
            },
        )
        _set(
            "sensor.clawdbot_test_battery_voltage",
            voltage,
            {
                "friendly_name": "Clawdbot Test Battery Voltage",
                "unit_of_measurement": "V",
                "icon": "mdi:flash",
            },
        )
        _set(
            "sensor.clawdbot_test_solar_w",
            solar_w,
            {
                "friendly_name": "Clawdbot Test Solar Power",
                "unit_of_measurement": "W",
                "icon": "mdi:solar-power",
            },
        )
        _set(
            "sensor.clawdbot_test_load_w",
            load_w,
            {
                "friendly_name": "Clawdbot Test Load Power",
                "unit_of_measurement": "W",
                "icon": "mdi:home-lightning-bolt",
            },
        )
        _set(
            "binary_sensor.clawdbot_test_connected",
            "on" if connected else "off",
            {
                "friendly_name": "Clawdbot Test Connected",
                "device_class": "connectivity",
            },
        )

        hass.data[DOMAIN]["dummy_entities"] = [
            "sensor.clawdbot_test_battery_soc",
            "sensor.clawdbot_test_battery_voltage",
            "sensor.clawdbot_test_solar_w",
            "sensor.clawdbot_test_load_w",
            "binary_sensor.clawdbot_test_connected",
        ]
        await _notify(
            "Clawdbot: dummy entities",
            "Created dummy entities for mapping QA (sensor.clawdbot_test_*).",
        )

    async def handle_clear_dummy_entities(call):
        ids = hass.data.get(DOMAIN, {}).get("dummy_entities") or []
        for eid in ids:
            try:
                hass.states.async_remove(eid)
            except Exception:
                pass
        hass.data[DOMAIN]["dummy_entities"] = []
        await _notify("Clawdbot: dummy entities", "Cleared dummy entities")

    # --- Derived / virtual sensors (Cockpit suggestions) ---

    def _to_float(state_val):
        try:
            if state_val is None:
                return None
            s = str(state_val).strip()
            if s in ("unknown", "unavailable", "None", ""):
                return None
            return float(s)
        except Exception:
            return None

    def _ema(prev, x, alpha: float):
        if x is None:
            return prev
        if prev is None:
            return x
        return (1.0 - alpha) * prev + alpha * x

    async def _derived_tick():
        cfg = hass.data.get(DOMAIN, {})
        mapping = cfg.get("mapping", {}) or {}
        rt = _runtime(hass)
        st = rt.get("derived_state")
        if not isinstance(st, dict):
            st = {}
            rt["derived_state"] = st

        solar_eid = mapping.get("solar")
        load_eid = mapping.get("load")

        solar = _to_float(hass.states.get(solar_eid).state) if solar_eid and hass.states.get(solar_eid) else None
        load = _to_float(hass.states.get(load_eid).state) if load_eid and hass.states.get(load_eid) else None

        # Rolling-ish averages (EMA) for quick v1 features.
        st["avg_load_15m"] = _ema(st.get("avg_load_15m"), load, alpha=0.02)
        st["avg_solar_15m"] = _ema(st.get("avg_solar_15m"), solar, alpha=0.02)

        # Trend (W per minute) using last sample.
        import time
        now = time.time()
        prev_t = st.get("last_t")
        prev_load = st.get("last_load")
        st["last_t"] = now
        st["last_load"] = load
        trend_w_per_min = None
        if prev_t and prev_load is not None and load is not None:
            dt_min = max(1e-6, (now - prev_t) / 60.0)
            trend_w_per_min = (load - prev_load) / dt_min

        # Always compute net power when possible.
        net = (solar - load) if (solar is not None and load is not None) else None

        def _set(eid: str, val, attrs: dict):
            hass.states.async_set(eid, "unknown" if val is None else str(round(val, 3) if isinstance(val, float) else val), attrs)

        _set(
            "sensor.clawdbot_net_power_w",
            net,
            {
                "friendly_name": "Clawdbot Net Power",
                "unit_of_measurement": "W",
                "icon": "mdi:transmission-tower",
                "uses": [solar_eid, load_eid],
                "formula": "solar_w - load_w",
            },
        )
        _set(
            "sensor.clawdbot_load_avg_15m_w",
            st.get("avg_load_15m"),
            {
                "friendly_name": "Clawdbot Load Avg (EMA ~15m)",
                "unit_of_measurement": "W",
                "icon": "mdi:chart-line",
                "uses": [load_eid],
            },
        )
        _set(
            "sensor.clawdbot_solar_avg_15m_w",
            st.get("avg_solar_15m"),
            {
                "friendly_name": "Clawdbot Solar Avg (EMA ~15m)",
                "unit_of_measurement": "W",
                "icon": "mdi:chart-line",
                "uses": [solar_eid],
            },
        )
        _set(
            "sensor.clawdbot_load_trend_w_per_min",
            trend_w_per_min,
            {
                "friendly_name": "Clawdbot Load Trend",
                "unit_of_measurement": "W/min",
                "icon": "mdi:trending-up",
                "uses": [load_eid],
            },
        )

        avg_load = st.get("avg_load_15m")
        load_spike = bool(load is not None and avg_load not in (None, 0) and load > (avg_load * 1.25))
        _set(
            "binary_sensor.clawdbot_load_spike",
            "on" if load_spike else "off",
            {
                "friendly_name": "Clawdbot Load Spike",
                "device_class": "problem",
                "uses": [load_eid],
                "rule": "load > avg_load_15m * 1.25",
            },
        )

        avg_solar = st.get("avg_solar_15m")
        solar_drop = bool(solar is not None and avg_solar not in (None, 0) and solar < (avg_solar * 0.6))
        _set(
            "binary_sensor.clawdbot_solar_drop",
            "on" if solar_drop else "off",
            {
                "friendly_name": "Clawdbot Solar Drop",
                "device_class": "problem",
                "uses": [solar_eid],
                "rule": "solar < avg_solar_15m * 0.6",
            },
        )

        rt["derived_last_update"] = now

    async def _derived_loop():
        import asyncio
        rt = _runtime(hass)
        while rt.get("derived_enabled"):
            try:
                await _derived_tick()
            except Exception:
                _LOGGER.exception("Derived sensors tick failed")
            await asyncio.sleep(10)

    async def _derived_set_enabled(enabled: bool):
        rt = _runtime(hass)
        store: Store = rt.get("derived_store")
        if store is None:
            raise HomeAssistantError("derived store not initialized")

        rt["derived_enabled"] = bool(enabled)
        cfg = rt.get("derived_cfg")
        if not isinstance(cfg, dict):
            cfg = {}
        cfg["enabled"] = bool(enabled)
        rt["derived_cfg"] = cfg
        await store.async_save(cfg)

        # Start/stop task
        task = rt.get("derived_task")
        if enabled:
            if task is None or getattr(task, "done", lambda: True)():
                rt["derived_task"] = hass.async_create_task(_derived_loop())
        else:
            if task is not None:
                try:
                    task.cancel()
                except Exception:
                    pass
                rt["derived_task"] = None

    async def handle_derived_sensors_set_enabled(call):
        enabled = bool(call.data.get("enabled"))
        await _derived_set_enabled(enabled)
        rt = _runtime(hass)
        return {
            "ok": True,
            "enabled": bool(rt.get("derived_enabled")),
        }

    async def handle_derived_sensors_status(call):
        rt = _runtime(hass)
        return {
            "ok": True,
            "enabled": bool(rt.get("derived_enabled")),
            "last_update": rt.get("derived_last_update"),
            "entities": [
                "sensor.clawdbot_net_power_w",
                "sensor.clawdbot_load_avg_15m_w",
                "sensor.clawdbot_solar_avg_15m_w",
                "sensor.clawdbot_load_trend_w_per_min",
                "binary_sensor.clawdbot_load_spike",
                "binary_sensor.clawdbot_solar_drop",
            ],
        }

    async def handle_derived_sensors_suggest(call):
        # Ensure we can compute a preview without enabling.
        try:
            await _derived_tick()
        except Exception:
            pass
        # Return current values (if present)
        def _get(eid: str):
            st = hass.states.get(eid)
            if not st:
                return None
            return {"entity_id": eid, "state": st.state, "attributes": dict(st.attributes)}

        return {
            "ok": True,
            "suggestions": [
                {
                    "entity_id": "sensor.clawdbot_net_power_w",
                    "why": "Shows whether you are net producing or consuming power right now.",
                    "uses": [hass.data.get(DOMAIN, {}).get("mapping", {}).get("solar"), hass.data.get(DOMAIN, {}).get("mapping", {}).get("load")],
                    "preview": _get("sensor.clawdbot_net_power_w"),
                },
                {
                    "entity_id": "sensor.clawdbot_load_avg_15m_w",
                    "why": "Smooths short-term noise to reveal your baseline household load.",
                    "uses": [hass.data.get(DOMAIN, {}).get("mapping", {}).get("load")],
                    "preview": _get("sensor.clawdbot_load_avg_15m_w"),
                },
                {
                    "entity_id": "sensor.clawdbot_solar_avg_15m_w",
                    "why": "Smooths solar output to help detect clouds / sustained changes.",
                    "uses": [hass.data.get(DOMAIN, {}).get("mapping", {}).get("solar")],
                    "preview": _get("sensor.clawdbot_solar_avg_15m_w"),
                },
                {
                    "entity_id": "sensor.clawdbot_load_trend_w_per_min",
                    "why": "Shows whether load is ramping up or down (good for detecting spikes early).",
                    "uses": [hass.data.get(DOMAIN, {}).get("mapping", {}).get("load")],
                    "preview": _get("sensor.clawdbot_load_trend_w_per_min"),
                },
                {
                    "entity_id": "binary_sensor.clawdbot_load_spike",
                    "why": "Flags sudden load spikes relative to baseline.",
                    "uses": [hass.data.get(DOMAIN, {}).get("mapping", {}).get("load")],
                    "preview": _get("binary_sensor.clawdbot_load_spike"),
                },
                {
                    "entity_id": "binary_sensor.clawdbot_solar_drop",
                    "why": "Flags sudden solar drops relative to baseline.",
                    "uses": [hass.data.get(DOMAIN, {}).get("mapping", {}).get("solar")],
                    "preview": _get("binary_sensor.clawdbot_solar_drop"),
                },
            ],
        }

    # Auto-start on boot if enabled
    if runtime.get("derived_enabled"):
        runtime["derived_task"] = hass.async_create_task(_derived_loop())

    hass.services.async_register(DOMAIN, "derived_sensors_set_enabled", handle_derived_sensors_set_enabled, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "derived_sensors_status", handle_derived_sensors_status, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "derived_sensors_suggest", handle_derived_sensors_suggest, supports_response=SupportsResponse.ONLY)

    # --- Agent 0 analysis services (token-safe) ---
    AGENT0_MAX_ENTITIES = 12
    AGENT0_MAX_HOURS = 72
    AGENT0_MAX_BUCKETS = 800
    AGENT0_MIN_BUCKET_MINUTES = 5

    def _state_info(eid: str):
        st = hass.states.get(eid) if eid else None
        if not st:
            return None
        attrs = st.attributes or {}
        return {
            "entity_id": st.entity_id,
            "state": st.state,
            "attributes": {
                "friendly_name": attrs.get("friendly_name"),
                "unit_of_measurement": attrs.get("unit_of_measurement"),
                "device_class": attrs.get("device_class"),
                "state_class": attrs.get("state_class"),
                "icon": attrs.get("icon"),
            },
            "last_updated": st.last_updated.isoformat() if st.last_updated else None,
            "last_changed": st.last_changed.isoformat() if st.last_changed else None,
        }

    def _agent0_hist_warmup(rt: dict) -> dict:
        hist = rt.get("agent0_hist")
        if not isinstance(hist, dict):
            return {"ready": False, "entities": {}}
        out = {"ready": False, "entities": {}}
        any_pts = False
        for eid, pts in hist.items():
            if not isinstance(pts, list) or not pts:
                continue
            any_pts = True
            try:
                oldest = pts[0][0]
                newest = pts[-1][0]
            except Exception:
                oldest = None
                newest = None
            out["entities"][eid] = {
                "points": len(pts),
                "oldest_ts": oldest,
                "newest_ts": newest,
            }
        out["ready"] = any_pts
        return out

    async def handle_agent0_get_context(call):
        cfg = hass.data.get(DOMAIN, {})
        mapping = cfg.get("mapping", {}) or {}
        rt = _runtime(hass)

        entity_ids = [
            mapping.get("soc"),
            mapping.get("voltage"),
            mapping.get("solar"),
            mapping.get("load"),
        ]
        entity_ids = [e for e in entity_ids if isinstance(e, str) and e]

        derived_entities = [
            "sensor.clawdbot_net_power_w",
            "sensor.clawdbot_load_avg_15m_w",
            "sensor.clawdbot_solar_avg_15m_w",
            "sensor.clawdbot_load_trend_w_per_min",
            "binary_sensor.clawdbot_load_spike",
            "binary_sensor.clawdbot_solar_drop",
        ]

        return {
            "ok": True,
            "timezone": hass.config.time_zone,
            "unit_system": {
                "name": getattr(hass.config.units, "name", None),
                "temperature_unit": getattr(hass.config.units, "temperature_unit", None),
            },
            "mapping": dict(mapping),
            "entities": [it for it in (_state_info(e) for e in entity_ids) if it],
            "derived": {
                "enabled": bool(rt.get("derived_enabled")),
                "last_update": rt.get("derived_last_update"),
                "entities": [it for it in (_state_info(e) for e in derived_entities) if it],
            },
            "buffer_warmup": _agent0_hist_warmup(rt),
        }

    async def handle_agent0_history_stats(call):
        # Inputs
        entity_ids = call.data.get("entity_ids") or []
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        if not isinstance(entity_ids, list):
            raise HomeAssistantError("entity_ids must be a list")
        entity_ids = [e for e in entity_ids if isinstance(e, str) and e]
        if len(entity_ids) > AGENT0_MAX_ENTITIES:
            raise HomeAssistantError(f"Too many entity_ids (max {AGENT0_MAX_ENTITIES})")

        period_hours = call.data.get("period_hours", 24)
        try:
            period_hours = float(period_hours)
        except Exception:
            raise HomeAssistantError("period_hours must be a number")
        if period_hours <= 0:
            raise HomeAssistantError("period_hours must be > 0")
        if period_hours > AGENT0_MAX_HOURS:
            period_hours = AGENT0_MAX_HOURS

        bucket_minutes = call.data.get("bucket_minutes", 15)
        try:
            bucket_minutes = int(bucket_minutes)
        except Exception:
            raise HomeAssistantError("bucket_minutes must be an integer")
        bucket_minutes = max(AGENT0_MIN_BUCKET_MINUTES, bucket_minutes)

        stat = str(call.data.get("stat", "mean")).lower().strip()
        if stat not in ("mean", "min", "max", "last"):
            raise HomeAssistantError("stat must be one of: mean|min|max|last")

        # Cap buckets
        import math
        buckets = int(math.ceil((period_hours * 60.0) / float(bucket_minutes)))
        if buckets > AGENT0_MAX_BUCKETS:
            buckets = AGENT0_MAX_BUCKETS
            period_hours = (buckets * bucket_minutes) / 60.0

        import time
        from homeassistant.util import dt as dt_util

        now = time.time()
        start_ts = now - (period_hours * 3600.0)
        bucket_s = bucket_minutes * 60

        rt = _runtime(hass)
        hist = rt.get("agent0_hist")
        if not isinstance(hist, dict):
            hist = {}

        # Default to mapped signals if entity_ids omitted.
        if not entity_ids:
            mapping = hass.data.get(DOMAIN, {}).get("mapping", {}) or {}
            entity_ids = [mapping.get("soc"), mapping.get("voltage"), mapping.get("solar"), mapping.get("load")]
            entity_ids = [e for e in entity_ids if isinstance(e, str) and e]

        out = {
            "ok": True,
            "start": dt_util.utc_from_timestamp(start_ts).isoformat(),
            "end": dt_util.utc_from_timestamp(now).isoformat(),
            "bucket_minutes": bucket_minutes,
            "stat": stat,
            "series": {},
            "warmup": {"entities": {}},
        }

        for eid in entity_ids:
            pts = hist.get(eid) or []
            if not isinstance(pts, list) or not pts:
                out["series"][eid] = []
                out["warmup"]["entities"][eid] = {"points": 0}
                continue

            # Warmup meta
            try:
                out["warmup"]["entities"][eid] = {
                    "points": len(pts),
                    "oldest_ts": pts[0][0],
                    "newest_ts": pts[-1][0],
                }
            except Exception:
                out["warmup"]["entities"][eid] = {"points": len(pts)}

            # Bucket values
            agg = [[] for _ in range(buckets)]
            for row in pts:
                try:
                    ts = float(row[0])
                    v = float(row[1])
                except Exception:
                    continue
                if ts < start_ts or ts > now:
                    continue
                idx = int((ts - start_ts) // bucket_s)
                if 0 <= idx < buckets:
                    agg[idx].append(v)

            series = []
            for i in range(buckets):
                vals = agg[i]
                vout = None
                if vals:
                    if stat == "min":
                        vout = min(vals)
                    elif stat == "max":
                        vout = max(vals)
                    elif stat == "last":
                        vout = vals[-1]
                    else:
                        vout = sum(vals) / float(len(vals))
                t_bucket_end = start_ts + ((i + 1) * bucket_s)
                series.append({"t": dt_util.utc_from_timestamp(t_bucket_end).isoformat(), "v": vout})

            out["series"][eid] = series

        return out

    # Agent0 history sampler loop
    async def _agent0_hist_prune(hist: dict, now_ts: float, retention_s: float, cap_points: int):
        if not isinstance(hist, dict):
            return
        cutoff = now_ts - retention_s
        for eid, pts in list(hist.items()):
            if not isinstance(pts, list):
                hist.pop(eid, None)
                continue
            # prune old
            try:
                while pts and float(pts[0][0]) < cutoff:
                    pts.pop(0)
            except Exception:
                pass
            # hard cap (keep newest)
            if len(pts) > cap_points:
                hist[eid] = pts[-cap_points:]

    async def _agent0_hist_persist(rt: dict):
        store: Store = rt.get("agent0_hist_store")
        hist = rt.get("agent0_hist")
        if store is None or not isinstance(hist, dict):
            return
        await store.async_save({"series": hist})
        rt["agent0_hist_last_persist"] = __import__("time").time()

    async def _agent0_hist_sampler_loop():
        import asyncio, time
        from homeassistant.util import dt as dt_util

        rt = _runtime(hass)
        # 30s sampling; 24h retention
        sample_s = 30
        retention_s = 24 * 3600
        cap_points = int((retention_s / sample_s) + 60)  # small slack
        persist_every_s = 5 * 60

        while True:
            try:
                cfg = hass.data.get(DOMAIN, {})
                mapping = cfg.get("mapping", {}) or {}
                eids = [mapping.get("soc"), mapping.get("voltage"), mapping.get("solar"), mapping.get("load")]
                eids = [e for e in eids if isinstance(e, str) and e]

                hist = rt.get("agent0_hist")
                if not isinstance(hist, dict):
                    hist = {}
                    rt["agent0_hist"] = hist

                now_ts = time.time()

                def _num(st):
                    try:
                        if st is None:
                            return None
                        s = str(st).strip()
                        if s in ("unknown", "unavailable", "None", ""):
                            return None
                        return float(s)
                    except Exception:
                        return None

                for eid in eids:
                    st = hass.states.get(eid)
                    if not st:
                        continue
                    v = _num(st.state)
                    if v is None:
                        continue
                    pts = hist.get(eid)
                    if not isinstance(pts, list):
                        pts = []
                        hist[eid] = pts
                    pts.append([now_ts, v])

                await _agent0_hist_prune(hist, now_ts, retention_s, cap_points)

                last_persist = rt.get("agent0_hist_last_persist")
                if (last_persist is None) or (now_ts - float(last_persist) >= persist_every_s):
                    try:
                        await _agent0_hist_persist(rt)
                    except Exception:
                        _LOGGER.exception("agent0 history persist failed")

            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("agent0 history sampler tick failed")

            await asyncio.sleep(sample_s)

    # Start sampler on boot
    try:
        rt0 = _runtime(hass)
        if rt0.get("agent0_hist_sampler_task") is None:
            rt0["agent0_hist_sampler_task"] = hass.async_create_task(_agent0_hist_sampler_loop())
    except Exception:
        _LOGGER.exception("Failed to start agent0 history sampler")

    hass.services.async_register(DOMAIN, "agent0_get_context", handle_agent0_get_context, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "agent0_history_stats", handle_agent0_history_stats, supports_response=SupportsResponse.ONLY)

    hass.services.async_register(DOMAIN, SERVICE_SEND_CHAT, handle_send_chat)
    hass.services.async_register(DOMAIN, "set_connection_overrides", handle_set_connection_overrides, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "reset_connection_overrides", handle_reset_connection_overrides, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, SERVICE_NOTIFY_EVENT, handle_notify_event)
    hass.services.async_register(DOMAIN, SERVICE_GATEWAY_TEST, handle_gateway_test, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, SERVICE_SET_MAPPING, handle_set_mapping)
    hass.services.async_register(DOMAIN, SERVICE_REFRESH_HOUSE_MEMORY, handle_refresh_house_memory)
    hass.services.async_register(DOMAIN, SERVICE_TOOLS_INVOKE, handle_tools_invoke)
    hass.services.async_register(DOMAIN, SERVICE_HA_GET_STATES, handle_ha_get_states)
    hass.services.async_register(DOMAIN, SERVICE_HA_CALL_SERVICE, handle_ha_call_service)
    hass.services.async_register(DOMAIN, SERVICE_CREATE_DUMMY_ENTITIES, handle_create_dummy_entities)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_DUMMY_ENTITIES, handle_clear_dummy_entities)
    hass.services.async_register(DOMAIN, "chat_append", handle_chat_append)
    hass.services.async_register(DOMAIN, SERVICE_CHAT_FETCH, handle_chat_fetch)
    hass.services.async_register(DOMAIN, SERVICE_CHAT_SEND, handle_chat_send)
    hass.services.async_register(DOMAIN, SERVICE_CHAT_HISTORY_DELTA, handle_chat_history_delta, supports_response=SupportsResponse.ONLY)

    hass.services.async_register(DOMAIN, "chat_new_session", handle_chat_new_session, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "chat_list_sessions", handle_chat_list_sessions, supports_response=SupportsResponse.ONLY)

    async def handle_chat_debug_stats(call):
        """Lightweight server-side stats for debugging duplication/filtering.

        Avoids returning full chat history objects.
        """
        cfg = hass.data.get(DOMAIN, {})
        rt = _runtime(hass)
        session_key = call.data.get("session_key") or rt.get("session_key") or DEFAULT_SESSION_KEY
        if not isinstance(session_key, str) or not session_key:
            session_key = DEFAULT_SESSION_KEY

        items = cfg.get("chat_history", []) or []
        if not isinstance(items, list):
            items = []
        items = [it for it in items if isinstance(it, dict) and it.get("session_key") == session_key]

        import re as _re
        # Only flag hard internal control/plumbing tokens (avoid false positives on normal text).
        bad_re = _re.compile(r"\bANNOUNCE_\w+\b|\bANNOUNCE_SKIP\b|\bNO_REPLY\b|\bHEARTBEAT_OK\b|agent-to-agent announce", _re.I)

        role_counts = {}
        fp = set()
        fp_missing = 0
        id_set = set()
        bad = 0
        bad_samples = []
        for it in items:
            role = it.get("role") or ""
            role_counts[role] = role_counts.get(role, 0) + 1
            if it.get("fingerprint"):
                fp.add(it.get("fingerprint"))
            else:
                fp_missing += 1
            if it.get("id"):
                id_set.add(it.get("id"))
            txt = it.get("text")
            if isinstance(txt, str) and bad_re.search(txt):
                bad += 1
                if len(bad_samples) < 3:
                    bad_samples.append(txt[:240])

        return {
            "ok": True,
            "session_key": session_key,
            "items": len(items),
            "role_counts": role_counts,
            "unique_ids": len(id_set),
            "unique_fingerprints": len(fp),
            "fingerprint_missing": fp_missing,
            "bad_marker_matches": bad,
            "bad_marker_samples": bad_samples,
        }

    hass.services.async_register(DOMAIN, "chat_debug_stats", handle_chat_debug_stats, supports_response=SupportsResponse.ONLY)

    async def handle_theme_set(call):
        cfg = hass.data.get(DOMAIN, {})
        store: Store = cfg.get("theme_store")
        if store is None:
            raise HomeAssistantError("theme store not initialized")

        preset = call.data.get("preset")
        auto = call.data.get("auto")

        current = cfg.get("theme", {})
        if not isinstance(current, dict):
            current = {}

        out = {
            "preset": current.get("preset") or "nebula",
            "auto": bool(current.get("auto")),
            "themes": current.get("themes") if isinstance(current.get("themes"), dict) else {},
        }
        if isinstance(preset, str) and preset.strip():
            out["preset"] = preset.strip()
        if auto is not None:
            out["auto"] = bool(auto)

        # Persist full cfg so custom themes remain
        await store.async_save({"preset": out["preset"], "auto": out["auto"], "themes": out["themes"]})
        cfg["theme"] = out
        return {"ok": True, "theme": out}

    async def handle_theme_reset(call):
        cfg = hass.data.get(DOMAIN, {})
        store: Store = cfg.get("theme_store")
        if store is None:
            raise HomeAssistantError("theme store not initialized")
        out = {"preset": "nebula", "auto": False, "themes": {}}
        await store.async_save({"preset": out["preset"], "auto": out["auto"], "themes": out["themes"]})
        cfg["theme"] = out
        return {"ok": True, "theme": out}

    async def handle_theme_upsert(call):
        cfg = hass.data.get(DOMAIN, {})
        store: Store = cfg.get("theme_store")
        if store is None:
            raise HomeAssistantError("theme store not initialized")

        key = call.data.get("key")
        theme_obj = call.data.get("theme")
        if not isinstance(key, str) or not key.strip():
            raise HomeAssistantError("key is required")
        if not isinstance(theme_obj, dict):
            raise HomeAssistantError("theme must be an object")

        current = cfg.get("theme", {})
        if not isinstance(current, dict):
            current = {"preset": "nebula", "auto": False, "themes": {}}
        themes = current.get("themes") if isinstance(current.get("themes"), dict) else {}
        themes[key.strip()] = theme_obj
        current["themes"] = themes
        cfg["theme"] = current

        await store.async_save({"preset": current.get("preset"), "auto": bool(current.get("auto")), "themes": themes})
        return {"ok": True, "themes": themes}

    async def handle_theme_delete(call):
        cfg = hass.data.get(DOMAIN, {})
        store: Store = cfg.get("theme_store")
        if store is None:
            raise HomeAssistantError("theme store not initialized")
        key = call.data.get("key")
        if not isinstance(key, str) or not key.strip():
            raise HomeAssistantError("key is required")
        current = cfg.get("theme", {})
        if not isinstance(current, dict):
            current = {"preset": "nebula", "auto": False, "themes": {}}
        themes = current.get("themes") if isinstance(current.get("themes"), dict) else {}
        themes.pop(key.strip(), None)
        current["themes"] = themes
        cfg["theme"] = current
        await store.async_save({"preset": current.get("preset"), "auto": bool(current.get("auto")), "themes": themes})
        return {"ok": True, "themes": themes}

    async def handle_theme_list(call):
        cfg = hass.data.get(DOMAIN, {})
        current = cfg.get("theme", {})
        if not isinstance(current, dict):
            current = {}
        return {"ok": True, "theme": current}

    hass.services.async_register(DOMAIN, "theme_set", handle_theme_set, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "theme_reset", handle_theme_reset, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "theme_upsert", handle_theme_upsert, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "theme_delete", handle_theme_delete, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "theme_list", handle_theme_list, supports_response=SupportsResponse.ONLY)

    # --- Dynamic Setup options registry (MVP) ---

    def _setup_key_ok(key: str) -> bool:
        key = (key or "").strip()
        if not key or len(key) > 128:
            return False
        allowed = ("ha.", "clawdbot.", "agent0.", "discord.", "stt.")
        return key.startswith(allowed)

    def _setup_mask_option(opt: dict) -> dict:
        if not isinstance(opt, dict):
            return {}
        out = dict(opt)
        typ = out.get("type")
        if typ == "secret":
            out["masked"] = True
            # never return secret value
            out.pop("value", None)
        return out

    async def _setup_save(cfg: dict):
        store: Store = cfg.get("setup_options_store")
        reg = cfg.get("setup_registry")
        if store is None or not isinstance(reg, dict):
            raise HomeAssistantError("setup options store not initialized")
        await store.async_save(reg)

    async def _setup_seed_defaults(cfg: dict):
        # Seed minimal keys if missing (do not overwrite existing values)
        import datetime as _dt

        opts = cfg.get("setup_options")
        reg = cfg.get("setup_registry")
        if not isinstance(opts, dict) or not isinstance(reg, dict):
            return

        now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")

        def ensure(key, label, typ, default=None, env=None, allowed=None):
            if key in opts:
                return
            opt = {
                "key": key,
                "label": label,
                "type": typ,
                "default": default,
                "env": env,
                "ui": {"group": "Dynamic", "order": 1000},
                "meta": {"created_ts": now, "updated_ts": now, "source": "system"},
            }
            if allowed is not None:
                opt["validation"] = {"allowed": allowed}
            opts[key] = opt

        ensure("ha.base_url.test", "HA base URL (test)", "url", env="test")
        ensure("ha.base_url.prod", "HA base URL (prod)", "url", env="prod")
        ensure("clawdbot.target_env", "Target environment", "select", default="test", allowed=["test", "prod"])
        ensure("agent0.state_push_enabled", "Agent0 state push enabled", "bool", default=True)
        ensure("discord.journal_channel_id", "Discord journal channel id", "string")

        # STT / Whisper options (MVP)
        ensure("stt.mode", "Speech to text mode", "select", default="native", allowed=["native", "whisper_openai"])
        ensure("stt.chunk_seconds", "STT chunk seconds", "number", default=5)
        ensure("stt.whisper_openai_api_key", "OpenAI API key (Whisper)", "secret")

        reg["options"] = opts
        await _setup_save(cfg)

    async def handle_setup_options_list(call):
        cfg = hass.data.get(DOMAIN, {})
        await _setup_seed_defaults(cfg)
        opts = cfg.get("setup_options")
        if not isinstance(opts, dict):
            opts = {}
        arr = []
        for k, opt in opts.items():
            if not isinstance(opt, dict):
                continue
            opt2 = dict(opt)
            opt2.setdefault("key", k)
            arr.append(_setup_mask_option(opt2))

        def _sort_key(o):
            ui = o.get("ui") if isinstance(o.get("ui"), dict) else {}
            group = ui.get("group") or ""
            order = ui.get("order")
            try:
                order = int(order)
            except Exception:
                order = 0
            return (str(group), order, str(o.get("key") or ""))

        arr.sort(key=_sort_key)
        return {"ok": True, "options": arr}

    async def handle_setup_option_define(call):
        cfg = hass.data.get(DOMAIN, {})
        await _setup_seed_defaults(cfg)
        reg = cfg.get("setup_registry")
        opts = cfg.get("setup_options")
        if not isinstance(reg, dict) or not isinstance(opts, dict):
            raise HomeAssistantError("setup registry not initialized")

        opt = call.data.get("option")
        if not isinstance(opt, dict):
            raise HomeAssistantError("option must be an object")

        key = opt.get("key")
        if not isinstance(key, str) or not _setup_key_ok(key):
            raise HomeAssistantError("invalid key")

        # caps
        if key not in opts and len(opts) >= 50:
            raise HomeAssistantError("too many setup options")

        typ = opt.get("type")
        if typ not in {"string", "url", "secret", "bool", "number", "select", "json"}:
            raise HomeAssistantError("invalid type")

        import datetime as _dt

        now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")

        current = opts.get(key) if isinstance(opts.get(key), dict) else {"key": key}
        merged = dict(current)

        # allowlist fields that can be updated via define
        for fld in (
            "label",
            "type",
            "default",
            "description",
            "placeholder",
            "scope",
            "env",
            "masked",
            "sensitive",
            "validation",
            "ui",
            "readOnly",
            "jsonSchema",
        ):
            if fld in opt:
                merged[fld] = opt.get(fld)

        meta = merged.get("meta") if isinstance(merged.get("meta"), dict) else {}
        if "created_ts" not in meta:
            meta["created_ts"] = now
        meta["updated_ts"] = now
        src = call.data.get("source")
        meta["source"] = str(src)[:40] if isinstance(src, str) and src.strip() else "agent"
        merged["meta"] = meta

        # do not overwrite existing user value unless unset
        if "value" in opt:
            if key not in opts or merged.get("value") is None:
                merged["value"] = opt.get("value")

        opts[key] = merged
        reg["options"] = opts
        await _setup_save(cfg)
        return {"ok": True}

    def _validate_setup_value(opt: dict, value):
        typ = opt.get("type")
        val_rules = opt.get("validation") if isinstance(opt.get("validation"), dict) else {}
        allowed = val_rules.get("allowed")
        if typ == "bool":
            if not isinstance(value, bool):
                raise HomeAssistantError("value must be boolean")
        elif typ == "number":
            if not isinstance(value, (int, float)):
                raise HomeAssistantError("value must be number")
            if isinstance(value, bool):
                raise HomeAssistantError("value must be number")
            # basic caps
            try:
                if float(value) < 0 or float(value) > 3600:
                    raise HomeAssistantError("value out of range")
            except Exception:
                raise HomeAssistantError("value must be number")
        elif typ in {"string", "url", "secret", "select", "json"}:
            # keep as-is; enforce size for strings
            if typ != "json" and not isinstance(value, str):
                raise HomeAssistantError("value must be string")
            if isinstance(value, str) and len(value) > 4096:
                raise HomeAssistantError("value too large")
        else:
            raise HomeAssistantError("invalid type")

        if isinstance(allowed, list) and typ == "select":
            if value not in allowed:
                raise HomeAssistantError("value not allowed")

    async def handle_setup_option_set(call):
        """Set a setup option value.

        Important: do not raise HomeAssistantError for validation failures; return
        `{ok:false,error}` so the panel can surface a friendly message.
        """
        cfg = hass.data.get(DOMAIN, {})
        await _setup_seed_defaults(cfg)
        reg = cfg.get("setup_registry")
        opts = cfg.get("setup_options")
        if not isinstance(reg, dict) or not isinstance(opts, dict):
            return {"ok": False, "error": "setup registry not initialized"}

        key = call.data.get("key")
        if not isinstance(key, str) or not _setup_key_ok(key):
            return {"ok": False, "error": "invalid key"}
        if key not in opts or not isinstance(opts.get(key), dict):
            return {"ok": False, "error": "unknown key"}

        opt = opts[key]
        if bool(opt.get("readOnly")):
            return {"ok": False, "error": "option is readOnly"}

        value = call.data.get("value")
        typ = opt.get("type")

        # Secret blank => NOOP
        if typ == "secret" and (value is None or (isinstance(value, str) and value.strip() == "")):
            return {"ok": True, "noop": True}

        try:
            _validate_setup_value(opt, value)
        except HomeAssistantError as e:
            return {"ok": False, "error": str(e)}
        except Exception:
            return {"ok": False, "error": "validation failed"}

        import datetime as _dt

        now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")
        opt["value"] = value
        meta = opt.get("meta") if isinstance(opt.get("meta"), dict) else {}
        meta["updated_ts"] = now
        src = call.data.get("source")
        meta["source"] = str(src)[:40] if isinstance(src, str) and src.strip() else "captain"
        opt["meta"] = meta
        opts[key] = opt
        reg["options"] = opts
        await _setup_save(cfg)
        cfg["setup_registry"] = reg
        cfg["setup_options"] = opts

        # Notify via HA event
        try:
            hass.bus.async_fire(
                "clawdbot_setup_option_changed",
                {
                    "key": key,
                    "updated_ts": now,
                    "source": meta.get("source"),
                    "masked": (typ == "secret"),
                    "env": opt.get("env"),
                },
            )
        except Exception:
            pass

        return {"ok": True}

    async def handle_setup_option_reset(call):
        cfg = hass.data.get(DOMAIN, {})
        await _setup_seed_defaults(cfg)
        reg = cfg.get("setup_registry")
        opts = cfg.get("setup_options")
        if not isinstance(reg, dict) or not isinstance(opts, dict):
            raise HomeAssistantError("setup registry not initialized")

        key = call.data.get("key")
        if not isinstance(key, str) or not _setup_key_ok(key):
            raise HomeAssistantError("invalid key")
        if key not in opts or not isinstance(opts.get(key), dict):
            raise HomeAssistantError("unknown key")

        clear_value = bool(call.data.get("clear_value"))
        opt = opts[key]
        if clear_value:
            opt.pop("value", None)
        else:
            if "default" in opt:
                opt["value"] = opt.get("default")
            else:
                opt.pop("value", None)

        opts[key] = opt
        reg["options"] = opts
        await _setup_save(cfg)
        return {"ok": True}

    async def handle_stt_whisper_health(call):
        cfg = hass.data.get(DOMAIN, {})
        opts = cfg.get("setup_options")
        configured = False
        if isinstance(opts, dict):
            opt = opts.get("stt.whisper_openai_api_key")
            if isinstance(opt, dict):
                v = opt.get("value")
                if isinstance(v, str) and v.strip():
                    configured = True
        return {"ok": True, "configured": configured}

    hass.services.async_register(DOMAIN, "setup_options_list", handle_setup_options_list, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "setup_option_define", handle_setup_option_define, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "setup_option_set", handle_setup_option_set, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "setup_option_reset", handle_setup_option_reset, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "stt_whisper_health", handle_stt_whisper_health, supports_response=SupportsResponse.ONLY)

    async def handle_journal_append(call):
        cfg = hass.data.get(DOMAIN, {})
        store: Store = cfg.get("journal_store")
        if store is None:
            raise HomeAssistantError("journal store not initialized")

        mood = call.data.get("mood")
        title = call.data.get("title")
        body = call.data.get("body")
        source = call.data.get("source")

        if body is None:
            raise HomeAssistantError("body is required")
        body = str(body)
        if not body.strip():
            raise HomeAssistantError("body is required")

        import datetime as _dt
        now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")
        item = {
            "ts": now,
            "mood": str(mood)[:40] if isinstance(mood, str) else None,
            "title": str(title)[:120] if isinstance(title, str) else None,
            "body": body[:6000],
            "source": str(source)[:40] if isinstance(source, str) else "agent",
        }

        items = cfg.get("journal", []) or []
        if not isinstance(items, list):
            items = []
        items.append(item)
        if len(items) > 200:
            items = items[-200:]
        await store.async_save(items)
        cfg["journal"] = items
        return {"ok": True}

    async def handle_journal_list(call):
        cfg = hass.data.get(DOMAIN, {})
        items = cfg.get("journal", []) or []
        if not isinstance(items, list):
            items = []
        limit = 10
        try:
            limit = int(call.data.get("limit", 10))
        except Exception:
            limit = 10
        if limit < 1:
            limit = 1
        if limit > 50:
            limit = 50
        return {"ok": True, "items": items[-limit:]}

    hass.services.async_register(DOMAIN, "journal_append", handle_journal_append, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "journal_list", handle_journal_list, supports_response=SupportsResponse.ONLY)

    async def handle_agent_profile_get(call):
        cfg = hass.data.get(DOMAIN, {})
        prof = cfg.get("agent_profile", {})
        if not isinstance(prof, dict):
            prof = {}
        return {"ok": True, "profile": prof}

    async def handle_agent_profile_set(call):
        cfg = hass.data.get(DOMAIN, {})
        store: Store = cfg.get("agent_profile_store")
        if store is None:
            raise HomeAssistantError("agent profile store not initialized")
        mood = call.data.get("mood")
        desc = call.data.get("description")
        if mood is not None and not isinstance(mood, str):
            raise HomeAssistantError("mood must be a string")
        if desc is not None and not isinstance(desc, str):
            raise HomeAssistantError("description must be a string")
        prof = cfg.get("agent_profile", {})
        if not isinstance(prof, dict):
            prof = {}
        if isinstance(mood, str) and mood.strip():
            prof["mood"] = mood.strip()[:24]
        if isinstance(desc, str) and desc.strip():
            prof["description"] = desc.strip()[:200]
        import datetime as _dt
        prof["updated_ts"] = _dt.datetime.now(tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")
        await store.async_save(prof)
        cfg["agent_profile"] = prof
        return {"ok": True, "profile": prof}

    async def handle_agent_state_get(call):
        cfg = hass.data.get(DOMAIN, {})
        prof = cfg.get("agent_profile", {})
        if not isinstance(prof, dict):
            prof = {}
        items = cfg.get("journal", []) or []
        if not isinstance(items, list):
            items = []
        latest = items[-1] if items else None
        return {"ok": True, "profile": prof, "latest_journal": latest}

    async def handle_agent_state_set(call):
        """Write agent-managed mood/description and optionally append a journal entry.

        This is intended to be called by Agent 0 (push hook).
        """
        cfg = hass.data.get(DOMAIN, {})
        prof_store: Store = cfg.get("agent_profile_store")
        journal_store: Store = cfg.get("journal_store")
        if prof_store is None or journal_store is None:
            raise HomeAssistantError("stores not initialized")

        mood = call.data.get("mood")
        desc = call.data.get("description")
        journal = call.data.get("journal")
        source = call.data.get("source")

        if mood is not None and not isinstance(mood, str):
            raise HomeAssistantError("mood must be a string")
        if desc is not None and not isinstance(desc, str):
            raise HomeAssistantError("description must be a string")

        import datetime as _dt
        now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")

        prof = cfg.get("agent_profile", {})
        if not isinstance(prof, dict):
            prof = {}
        if isinstance(mood, str) and mood.strip():
            prof["mood"] = mood.strip()[:24]
        if isinstance(desc, str) and desc.strip():
            prof["description"] = desc.strip()[:200]
        prof["updated_ts"] = now
        if isinstance(source, str) and source.strip():
            prof["source"] = source.strip()[:40]

        await prof_store.async_save(prof)
        cfg["agent_profile"] = prof

        # Notify listeners (panel) that agent state changed.
        try:
            hass.bus.async_fire(
                "clawdbot_agent_state_changed",
                {
                    "updated_ts": now,
                    "source": prof.get("source"),
                    "mood": prof.get("mood"),
                },
            )
        except Exception:
            pass

        appended = False
        if isinstance(journal, dict):
            title = journal.get("title")
            body = journal.get("body")
            jmood = journal.get("mood")
            if isinstance(body, str) and body.strip():
                items = cfg.get("journal", []) or []
                if not isinstance(items, list):
                    items = []
                items.append(
                    {
                        "ts": now,
                        "mood": (str(jmood)[:40] if isinstance(jmood, str) else (prof.get("mood") or None)),
                        "title": (str(title)[:120] if isinstance(title, str) else None),
                        "body": str(body)[:6000],
                        "source": (str(source)[:40] if isinstance(source, str) else "agent"),
                    }
                )
                if len(items) > 200:
                    items = items[-200:]
                await journal_store.async_save(items)
                cfg["journal"] = items
                appended = True

        return {"ok": True, "profile": prof, "journal_appended": appended}

    async def handle_agent_state_reset(call):
        """Reset agent profile and (optionally) clear journal entries.

        Intended to clear test values like TEST_PUSH.
        """
        cfg = hass.data.get(DOMAIN, {})
        prof_store: Store = cfg.get("agent_profile_store")
        journal_store: Store = cfg.get("journal_store")
        if prof_store is None or journal_store is None:
            raise HomeAssistantError("stores not initialized")

        clear_journal = bool(call.data.get("clear_journal"))

        prof = {}
        await prof_store.async_save(prof)
        cfg["agent_profile"] = prof

        cleared = {"profile": True, "journal": False}
        if clear_journal:
            await journal_store.async_save([])
            cfg["journal"] = []
            cleared["journal"] = True

        return {"ok": True, "cleared": cleared}

    async def handle_avatar_prompt_set(call):
        cfg = hass.data.get(DOMAIN, {})
        store: Store = cfg.get("avatar_store")
        if store is None:
            raise HomeAssistantError("avatar store not initialized")

        agent_id = call.data.get("agent_id") or "agent0"
        text = call.data.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HomeAssistantError("text is required")
        text = text.strip()

        avatar = cfg.get("avatar")
        if not isinstance(avatar, dict):
            avatar = {}
        avatar["agent_id"] = str(agent_id)
        avatar["prompt_text"] = text
        import datetime as _dt
        avatar["prompt_updated_ts"] = _dt.datetime.now(tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")

        await store.async_save(avatar)
        cfg["avatar"] = avatar
        return {"ok": True}

    async def handle_avatar_generate_request(call):
        cfg = hass.data.get(DOMAIN, {})
        store: Store = cfg.get("avatar_store")
        if store is None:
            raise HomeAssistantError("avatar store not initialized")

        agent_id = call.data.get("agent_id") or "agent0"
        prompt = call.data.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise HomeAssistantError("prompt is required")

        import datetime as _dt
        import uuid as _uuid

        request_id = call.data.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            request_id = _uuid.uuid4().hex

        now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")

        avatar = cfg.get("avatar")
        if not isinstance(avatar, dict):
            avatar = {}
        avatar.update(
            {
                "agent_id": str(agent_id),
                "last_request_id": request_id,
                "last_prompt": str(prompt),
                "last_request_ts": now,
            }
        )
        await store.async_save(avatar)
        cfg["avatar"] = avatar

        # Emit event for Agent0/host listener (preferred transport; avoids chat delivery issues)
        webhook_path = None
        webhook_url = None
        try:
            from homeassistant.components import webhook

            wh_store: Store = cfg.get("avatar_webhook_store")
            wh_data = cfg.get("avatar_webhook")
            if wh_store is not None and isinstance(wh_data, dict):
                webhook_id = wh_data.get("webhook_id")
                if not isinstance(webhook_id, str) or not webhook_id:
                    webhook_id = webhook.async_generate_id()
                    wh_data = {"webhook_id": webhook_id}
                    await wh_store.async_save(wh_data)
                    cfg["avatar_webhook"] = wh_data
                webhook_path = f"/api/webhook/{webhook_id}"

            # Optional: include full URL using dynamic setup base_url (env-aware)
            opts = cfg.get("setup_options")
            base_url = None
            if isinstance(opts, dict):
                env_opt = opts.get("clawdbot.target_env")
                env = None
                if isinstance(env_opt, dict):
                    v = env_opt.get("value")
                    if isinstance(v, str) and v.strip():
                        env = v.strip()
                base_key = "ha.base_url.test" if env == "test" else "ha.base_url.prod" if env == "prod" else None
                if base_key:
                    b = opts.get(base_key)
                    if isinstance(b, dict):
                        bv = b.get("value")
                        if isinstance(bv, str) and bv.strip():
                            base_url = bv.strip().rstrip("/")
            if base_url and webhook_path:
                webhook_url = f"{base_url}{webhook_path}"
        except Exception:
            pass

        try:
            hass.bus.async_fire(
                "clawdbot_avatar_generate_requested",
                {
                    "request_id": request_id,
                    "agent_id": str(agent_id),
                    "prompt": str(prompt),
                    "webhook_path": webhook_path,
                    "webhook_url": webhook_url,
                    "ts": now,
                },
            )
        except Exception:
            pass

        return {"ok": True, "request_id": request_id, "webhook_path": webhook_path, "webhook_url": webhook_url}

    async def handle_avatar_apply(call):
        """Promote a stored preview (by request_id) to the active avatar."""
        cfg = hass.data.get(DOMAIN, {})
        store: Store = cfg.get("avatar_store")
        if store is None:
            raise HomeAssistantError("avatar store not initialized")

        request_id = call.data.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            raise HomeAssistantError("request_id is required")
        request_id = request_id.strip()

        avatar = cfg.get("avatar")
        if not isinstance(avatar, dict):
            avatar = {}
        previews = avatar.get("previews")
        if not isinstance(previews, dict):
            raise HomeAssistantError("no previews")
        item = previews.get(request_id)
        if not isinstance(item, dict):
            raise HomeAssistantError("preview not found")
        png_b64 = item.get("png_b64")
        if not isinstance(png_b64, str) or not png_b64:
            raise HomeAssistantError("preview missing png")

        import datetime as _dt

        avatar["active_png_b64"] = png_b64
        avatar["active_updated_ts"] = _dt.datetime.now(tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")
        avatar["active_request_id"] = request_id

        await store.async_save(avatar)
        cfg["avatar"] = avatar

        try:
            hass.bus.async_fire(
                "clawdbot_avatar_changed",
                {
                    "agent_id": avatar.get("agent_id") or "agent0",
                    "request_id": request_id,
                    "active_updated_ts": avatar.get("active_updated_ts"),
                },
            )
        except Exception:
            pass

        return {"ok": True, "request_id": request_id}

    async def handle_avatar_generate_dispatch(call):
        """Dispatch avatar generation to Agent0 via Gateway sessions_spawn.

        Avoids relying on HA internal event bus (not reachable from Agent0 host) and avoids flaky chat sessions.
        """
        hass = call.hass

        agent_id = call.data.get("agent_id") or "agent0"
        prompt = call.data.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise HomeAssistantError("prompt is required")

        # OpenClaw gateway agent id to execute the generation (default: main)
        agent_target = call.data.get("agent_target")
        if not isinstance(agent_target, str) or not agent_target.strip():
            agent_target = "main"
        agent_target = agent_target.strip()

        ha_origin = call.data.get("ha_origin")
        if ha_origin is not None and not isinstance(ha_origin, str):
            ha_origin = None
        if isinstance(ha_origin, str):
            ha_origin = ha_origin.strip().rstrip("/")
            if not ha_origin.startswith("http://") and not ha_origin.startswith("https://"):
                ha_origin = None

        # Generate request_id + webhook path (also records prompt in Store)
        class _Call:
            __slots__ = ("data", "hass")

            def __init__(self, hass, data):
                self.hass = hass
                self.data = data

        gen = await handle_avatar_generate_request(_Call(hass, {"agent_id": agent_id, "prompt": prompt}))
        request_id = gen.get("request_id") if isinstance(gen, dict) else None
        webhook_path = gen.get("webhook_path") if isinstance(gen, dict) else None
        webhook_url = gen.get("webhook_url") if isinstance(gen, dict) else None

        if not webhook_url and isinstance(ha_origin, str) and isinstance(webhook_path, str) and webhook_path:
            webhook_url = f"{ha_origin}{webhook_path}"

        if not isinstance(webhook_url, str) or not webhook_url:
            # Still allow dispatch (Agent0 can reconstruct from path), but return useful diagnostics.
            webhook_url = None

        # Spawn Agent0 run on gateway
        session, gateway_origin, token, _default_session_key = _runtime_gateway_parts(hass)

        task = "\n".join(
            [
                "Generate a 1:1 profile avatar image using nano-banana-pro, then POST png_b64 to the webhook.",
                f"agent_id: {agent_id}",
                f"request_id: {request_id}",
                f"webhook_url: {webhook_url or ''}",
                f"webhook_path: {webhook_path or ''}",
                "",
                "Prompt:",
                str(prompt).strip(),
            ]
        )

        # Prefer deterministic delivery to Agent0 main session (so it runs generation every time),
        # falling back to sessions_spawn if no sessionKey is configured.
        opts = hass.data.get(DOMAIN, {}).get("setup_options")
        dispatch_session_key = None
        if isinstance(opts, dict):
            o = opts.get("agent0.dispatch_session_key")
            if isinstance(o, dict):
                v = o.get("value")
                if isinstance(v, str) and v.strip():
                    dispatch_session_key = v.strip()
        # Last-resort default for our dev Discord channel (can be overridden via setup option).
        if not dispatch_session_key:
            dispatch_session_key = "agent:main:discord:channel:1467991467363405834"

        run_id = None
        dispatched_via = None

        if dispatch_session_key:
            payload = {
                "tool": "sessions_send",
                "args": {"sessionKey": dispatch_session_key, "message": task},
            }
            res = await _gw_post(session, gateway_origin + "/tools/invoke", token, payload)
            dispatched_via = "sessions_send"
            try:
                if isinstance(res, dict):
                    run_id = res.get("runId") or res.get("result", {}).get("runId")
            except Exception:
                run_id = None
        else:
            payload = {
                "tool": "sessions_spawn",
                "args": {
                    "task": task,
                    "label": f"avatar-generate:{request_id}",
                    "agentId": str(agent_target),
                    "cleanup": "keep",
                },
            }
            res = await _gw_post(session, gateway_origin + "/tools/invoke", token, payload)
            dispatched_via = "sessions_spawn"
            try:
                if isinstance(res, dict):
                    run_id = res.get("runId") or res.get("result", {}).get("runId")
            except Exception:
                run_id = None

        return {
            "ok": True,
            "request_id": request_id,
            "webhook_url": webhook_url,
            "webhook_path": webhook_path,
            "run_id": run_id,
            "dispatched_agent_id": agent_target,
            "dispatched_via": dispatched_via,
            "dispatch_session_key": dispatch_session_key,
        }

    async def handle_avatar_webhook_get(call):
        cfg = hass.data.get(DOMAIN, {})
        store: Store = cfg.get("avatar_webhook_store")
        data = cfg.get("avatar_webhook")
        if store is None or not isinstance(data, dict):
            raise HomeAssistantError("avatar webhook store not initialized")

        from homeassistant.components import webhook

        webhook_id = data.get("webhook_id")
        if not isinstance(webhook_id, str) or not webhook_id:
            webhook_id = webhook.async_generate_id()
            data = {"webhook_id": webhook_id}
            await store.async_save(data)
            cfg["avatar_webhook"] = data

        return {"ok": True, "webhook_id": webhook_id, "path": f"/api/webhook/{webhook_id}"}

    async def handle_avatar_set_b64(call):
        cfg = hass.data.get(DOMAIN, {})
        store: Store = cfg.get("avatar_store")
        if store is None:
            raise HomeAssistantError("avatar store not initialized")

        import base64
        import datetime as _dt

        agent_id = call.data.get("agent_id") or "agent0"
        request_id = call.data.get("request_id")
        if request_id is not None and not isinstance(request_id, str):
            request_id = None
        if isinstance(request_id, str):
            request_id = request_id.strip() or None

        png_b64 = call.data.get("png_b64")
        if not isinstance(png_b64, str) or not png_b64.strip():
            raise HomeAssistantError("png_b64 is required")
        b64 = png_b64.strip()
        if b64.startswith("data:"):
            try:
                b64 = b64.split(",", 1)[1]
            except Exception:
                raise HomeAssistantError("invalid data url")
        try:
            raw = base64.b64decode(b64)
        except Exception:
            raise HomeAssistantError("invalid base64")

        # Size cap (bytes). 1K avatars can be ~200KB–1.3MB depending on content.
        if len(raw) > 1_700_000:
            raise HomeAssistantError("image too large")

        avatar = cfg.get("avatar")
        if not isinstance(avatar, dict):
            avatar = {}

        # Store preview keyed by request_id when present.
        if request_id:
            previews = avatar.get("previews")
            if not isinstance(previews, dict):
                previews = {}
            previews[request_id] = {
                "png_b64": b64,
                "ts": _dt.datetime.now(tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            # keep last N previews
            try:
                if len(previews) > 6:
                    keys = list(previews.keys())
                    # sort by ts when possible
                    keys.sort(key=lambda k: (previews.get(k, {}) or {}).get("ts") or "")
                    for k in keys[:-6]:
                        previews.pop(k, None)
            except Exception:
                pass
            avatar["previews"] = previews
            avatar["last_preview_request_id"] = request_id
            avatar["last_preview_ts"] = previews.get(request_id, {}).get("ts")

        # Back-compat: if no request_id provided, treat as active.
        if not request_id:
            avatar["active_png_b64"] = b64
            avatar["active_updated_ts"] = _dt.datetime.now(tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")

        avatar["agent_id"] = str(agent_id)
        await store.async_save(avatar)
        cfg["avatar"] = avatar

        # Fire event for UI refresh (preview or active)
        try:
            hass.bus.async_fire(
                "clawdbot_avatar_changed",
                {
                    "agent_id": str(agent_id),
                    "request_id": request_id,
                    "active_updated_ts": avatar.get("active_updated_ts"),
                    "last_preview_request_id": avatar.get("last_preview_request_id"),
                },
            )
        except Exception:
            pass

        return {"ok": True, "request_id": request_id}

    async def handle_agent_pulse(call):
        """Pulse is now read-only: refresh the latest agent-managed state."""
        return await handle_agent_state_get(call)

    async def handle_agent_state_webhook_get(call):
        """Return the webhook id/path for Agent 0 to push state updates cross-host."""
        cfg = hass.data.get(DOMAIN, {})
        store: Store = cfg.get("agent_state_webhook_store")
        data = cfg.get("agent_state_webhook", {})
        if store is None or not isinstance(data, dict):
            raise HomeAssistantError("agent state webhook store not initialized")

        webhook_id = data.get("webhook_id")
        if not isinstance(webhook_id, str) or not webhook_id:
            try:
                from homeassistant.components import webhook

                webhook_id = webhook.async_generate_id()
            except Exception:
                # fallback; HA should have webhook component
                import secrets

                webhook_id = secrets.token_hex(32)
            data = {"webhook_id": webhook_id}
            await store.async_save(data)
            cfg["agent_state_webhook"] = data

        # Only return the id + path; full URL depends on HA external_url/internal_url.
        return {"ok": True, "webhook_id": webhook_id, "path": f"/api/webhook/{webhook_id}"}

    hass.services.async_register(DOMAIN, "agent_profile_get", handle_agent_profile_get, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "agent_profile_set", handle_agent_profile_set, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "agent_state_get", handle_agent_state_get, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "agent_state_set", handle_agent_state_set, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "agent_state_reset", handle_agent_state_reset, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "agent_state_webhook_get", handle_agent_state_webhook_get, supports_response=SupportsResponse.ONLY)

    hass.services.async_register(DOMAIN, "avatar_prompt_set", handle_avatar_prompt_set, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "avatar_generate_request", handle_avatar_generate_request, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "avatar_generate_dispatch", handle_avatar_generate_dispatch, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "avatar_apply", handle_avatar_apply, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "avatar_webhook_get", handle_avatar_webhook_get, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "avatar_set_b64", handle_avatar_set_b64, supports_response=SupportsResponse.ONLY)

    # Back-compat: pulse now just refreshes state (read-only)
    hass.services.async_register(DOMAIN, "agent_pulse", handle_agent_pulse, supports_response=SupportsResponse.ONLY)

    async def handle_chat_store_sanitize(call):
        """Sanitize chat store for a session: remove control/plumbing lines and dedupe."""
        cfg = hass.data.get(DOMAIN, {})
        store: Store = cfg.get("chat_store")
        if store is None:
            raise HomeAssistantError("chat history store not initialized")
        rt = _runtime(hass)
        session_key = call.data.get("session_key") or rt.get("session_key") or DEFAULT_SESSION_KEY
        if not isinstance(session_key, str) or not session_key:
            session_key = DEFAULT_SESSION_KEY

        items = await store.async_load() or []
        if not isinstance(items, list):
            items = []
        items = [it for it in items if isinstance(it, dict)]

        import re as _re
        bad_re = _re.compile(r"\bANNOUNCE_\w+\b|\bNO_REPLY\b|\bHEARTBEAT_OK\b|agent-to-agent announce", _re.I)
        ws_re = _re.compile(r"\s+")

        def _norm(t: str) -> str:
            return ws_re.sub(" ", (t or "")).strip()

        def _fp(session: str, role: str, text: str, ts: str) -> str:
            import hashlib
            bucket = 0
            try:
                from homeassistant.util import dt as dt_util

                dt_obj = dt_util.parse_datetime(str(ts).replace("Z", "+00:00"))
                if dt_obj is not None:
                    bucket = int(dt_obj.timestamp() // 2)
            except Exception:
                pass
            base = f"{session}|{role}|{_norm(text)}|{bucket}"
            return hashlib.sha256(base.encode("utf-8")).hexdigest()

        # Keep items from other sessions untouched; sanitize only selected session.
        kept_other = [it for it in items if it.get("session_key") != session_key]
        target = [it for it in items if it.get("session_key") == session_key]

        out = []
        seen = set()
        removed_bad = 0
        removed_dup = 0
        for it in target:
            txt = it.get("text")
            if isinstance(txt, str) and bad_re.search(txt):
                removed_bad += 1
                continue
            fp = it.get("fingerprint")
            if not fp:
                try:
                    fp = _fp(session_key, it.get("role") or "", txt or "", it.get("ts") or "")
                    it["fingerprint"] = fp
                except Exception:
                    fp = None
            if fp and fp in seen:
                removed_dup += 1
                continue
            if fp:
                seen.add(fp)
            out.append(it)

        merged = kept_other + out
        # Keep last 500 overall (consistent with other trims)
        merged = merged[-500:]
        await store.async_save(merged)
        cfg["chat_history"] = merged

        return {
            "ok": True,
            "session_key": session_key,
            "removed_bad": removed_bad,
            "removed_dup": removed_dup,
            "kept": len(out),
        }

    hass.services.async_register(DOMAIN, "chat_store_sanitize", handle_chat_store_sanitize, supports_response=SupportsResponse.ONLY)

    async def handle_build_info(call):
        # For deployment verification (no secrets)
        services = hass.services.async_services().get(DOMAIN, {})
        rt = _runtime(hass)
        return {
            "ok": True,
            # Build ids:
            # - panel_build_id: used for /local/clawdbot-panel.js?v=...
            # - integration_build_id: python-side build stamp (commit sha)
            "panel_build_id": PANEL_BUILD_ID,
            "integration_build_id": INTEGRATION_BUILD_ID,
            "gateway_origin": rt.get("gateway_origin"),
            # Instrumentation: prove what HA actually loaded
            "config_dir": hass.config.config_dir,
            "integration_file": __file__,
            "services": sorted(list(services.keys())),
        }

    hass.services.async_register(DOMAIN, "build_info", handle_build_info, supports_response=SupportsResponse.ONLY)

    hass.services.async_register(DOMAIN, SERVICE_SESSIONS_LIST, handle_sessions_list, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, SERVICE_SESSIONS_SPAWN, handle_sessions_spawn, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, SERVICE_SESSION_STATUS_GET, handle_session_status_get, supports_response=SupportsResponse.ONLY)
    _LOGGER.info("Registering service: %s.%s", DOMAIN, SERVICE_CHAT_POLL)
    # Fire-and-forget: panel calls this service; backend updates Store. No service response needed.
    hass.services.async_register(DOMAIN, SERVICE_CHAT_POLL, handle_chat_poll)

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
