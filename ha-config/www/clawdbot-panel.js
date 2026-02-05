// Clawdbot panel JS (served by HA; avoids inline-script CSP issues)
// Marker visible to external debuggers even if init fails early.
window.__clawdbotPanelInit = 'booting';
window.__clawdbotPanelInitError = null;
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
    updateLoadOlderTop();

    if (preserveScroll) {
      const nextScrollHeight = list.scrollHeight;
      list.scrollTop = nextScrollHeight - prevScrollHeight + prevScrollTop;
    } else if (shouldAutoScroll || wasAtBottom) {
      list.scrollTop = list.scrollHeight;
    }
  }

  async function refreshBuildInfo(){
    const el = qs('#buildInfo');
    if (!el) return;
    try{
      const resp = await callServiceResponse('clawdbot','build_info',{});
      const data = (resp && resp.response) ? resp.response : resp;
      const r = data && data.result ? data.result : data;
      const p = r && r.panel_build_id ? String(r.panel_build_id) : '—';
      const i = r && r.integration_build_id ? String(r.integration_build_id) : '—';
      const g = r && r.gateway_origin ? String(r.gateway_origin) : '';
      el.textContent = `Build: panel ${p} · integration ${i}${g ? ` · gateway ${g}` : ''}`;
    } catch(e){
      el.textContent = '';
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
    try{ refreshBuildInfo(); } catch(e){}
  }
  const THEMES = {
    nebula: { name:'Nebula', a:'#00f5ff', b:'#7b2cff', c:'#ff3e8e', bg1:'rgba(0,245,255,.28)', bg2:'rgba(123,44,255,.28)', bg3:'rgba(255,62,142,.20)', glow:'rgba(0,245,255,.48)' },
    aurora: { name:'Aurora', a:'#3cffb4', b:'#00a6ff', c:'#b6ff3c', bg1:'rgba(60,255,180,.18)', bg2:'rgba(0,166,255,.22)', bg3:'rgba(182,255,60,.14)', glow:'rgba(60,255,180,.34)' },
    deep_ocean: { name:'Deep Ocean', a:'#00d4ff', b:'#0047ff', c:'#6a00ff', bg1:'rgba(0,212,255,.18)', bg2:'rgba(0,71,255,.24)', bg3:'rgba(106,0,255,.16)', glow:'rgba(0,212,255,.34)' },
    solar_flare: { name:'Solar Flare', a:'#ffb300', b:'#ff2d95', c:'#ff6b00', bg1:'rgba(255,179,0,.18)', bg2:'rgba(255,45,149,.24)', bg3:'rgba(255,107,0,.16)', glow:'rgba(255,179,0,.32)' },
    crimson_night: { name:'Crimson Night', a:'#ff2b2b', b:'#5b2bff', c:'#ff2da1', bg1:'rgba(255,43,43,.18)', bg2:'rgba(91,43,255,.24)', bg3:'rgba(255,45,161,.16)', glow:'rgba(255,43,43,.32)' },
  };

  function applyThemePreset(key, {silent=false, mood=null}={}){
    const t = THEMES[key] || THEMES.nebula;
    const root = document.documentElement;
    root.style.setProperty('--claw-accent-a', t.a);
    root.style.setProperty('--claw-accent-b', t.b);
    root.style.setProperty('--claw-accent-c', t.c);
    root.style.setProperty('--claw-bg-1', t.bg1);
    root.style.setProperty('--claw-bg-2', t.bg2);
    root.style.setProperty('--claw-bg-3', t.bg3);
    root.style.setProperty('--claw-btn-glow', t.glow);
    // Surface tint deliberately uses accent-c for contrast vs page bg
    root.style.setProperty('--claw-surface-tint', `color-mix(in srgb, ${t.c} 22%, transparent)`);
    try{
      const prev = document.getElementById('themePreview');
      if (prev) prev.style.background = `linear-gradient(120deg, color-mix(in srgb, ${t.a} 22%, transparent), color-mix(in srgb, ${t.b} 18%, transparent)), color-mix(in srgb, var(--ha-card-background, var(--card-background-color)) 85%, transparent)`;
    } catch(e){}
    if (!silent) toast(`Theme: ${t.name}${mood ? ` (mood: ${mood})` : ''}`);
  }

  function fillThemeInputs(){
    const cfg = (window.__CLAWDBOT_CONFIG__ || {});
    const theme = (cfg.theme || {});
    const sel = document.getElementById('themePreset');
    const auto = document.getElementById('themeAuto');
    if (sel && !sel.__filled) {
      sel.__filled = true;
      sel.innerHTML = '';
      for (const [k,v] of Object.entries(THEMES)) {
        const o = document.createElement('option');
        o.value = k;
        o.textContent = v.name;
        sel.appendChild(o);
      }
    }
    if (sel) sel.value = theme.preset || 'nebula';
    if (auto) auto.checked = !!theme.auto;
    applyThemePreset(theme.preset || 'nebula', {silent:true});
  }

  async function saveTheme(){
    const sel = document.getElementById('themePreset');
    const auto = document.getElementById('themeAuto');
    const result = document.getElementById('themeResult');
    const preset = sel ? sel.value : 'nebula';
    const isAuto = auto ? !!auto.checked : false;
    if (result) result.textContent = 'saving…';
    const resp = await callServiceResponse('clawdbot','theme_set',{preset, auto:isAuto});
    const data = (resp && resp.response) ? resp.response : resp;
    const r = data && data.result ? data.result : data;
    if (r && r.theme) {
      window.__CLAWDBOT_CONFIG__.theme = r.theme;
      fillThemeInputs();
      if (result) result.textContent = 'ok';
      applyThemePreset(r.theme.preset || preset, {silent:false});
    } else {
      if (result) result.textContent = 'error';
    }
  }

  async function resetTheme(){
    const result = document.getElementById('themeResult');
    if (result) result.textContent = 'resetting…';
    const resp = await callServiceResponse('clawdbot','theme_reset',{});
    const data = (resp && resp.response) ? resp.response : resp;
    const r = data && data.result ? data.result : data;
    if (r && r.theme) {
      window.__CLAWDBOT_CONFIG__.theme = r.theme;
      fillThemeInputs();
      if (result) result.textContent = 'ok';
      applyThemePreset(r.theme.preset || 'nebula', {silent:false});
    } else {
      if (result) result.textContent = 'error';
    }
  }

  function moodThemeKey(hass){
    // Deterministic + low-chatter: based on gateway/derived + time-of-day
    const cfg = (window.__CLAWDBOT_CONFIG__ || {});
    const theme = cfg.theme || {};
    if (!theme.auto) return theme.preset || 'nebula';
    const h = (new Date()).getHours();
    // night
    if (h >= 22 || h < 6) return 'crimson_night';
    // day
    return 'aurora';
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
      toast(kind === 'reset' ? 'Reset overrides to YAML defaults' : 'Saved overrides');
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
      const resp = await callServiceResponse('clawdbot','chat_list_sessions', {});
      const data = (resp && resp.response) ? resp.response : resp;

      const r = data && data.result ? data.result : data;
      const arr = (r && Array.isArray(r.items)) ? r.items : [];
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
      // Ensure there's always a visible value even if the list call fails.
      const fallback = current || (window.__CLAWDBOT_CONFIG__ && (window.__CLAWDBOT_CONFIG__.session_key)) || 'main';
      if (fallback) { sel.appendChild(mkOpt(fallback, fallback)); seen.add(fallback); }
      for (const s of arr){
        const key = s && (s.session_key || s.sessionKey || s.key || s.id);
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

  function renderEntityConfig(hass){
    // If missing mappings, compute suggestions and preview them (not persisted until Confirm all).
    const current = mappingWithDefaults();
    const suggested = computeAutoFill(hass);

    const setTxt = (id, txt) => { const el = document.getElementById(id); if (el) el.textContent = txt || '—'; };

    const info = (eid, isSuggested) => {
      if (!eid) return { name: 'Not set', meta: '—' };
      const st = hass && hass.states ? hass.states[eid] : null;
      const name = st && st.attributes && st.attributes.friendly_name ? String(st.attributes.friendly_name) : eid;
      const unit = st && st.attributes && st.attributes.unit_of_measurement ? String(st.attributes.unit_of_measurement) : '';
      const value = st ? String(st.state) : '—';
      const sug = isSuggested ? ' · Suggested' : '';
      return {
        name,
        meta: `${eid}${st ? ` · ${value}${unit ? ' '+unit : ''}` : ''}${sug}`,
      };
    };

    const show = (field, nameId, metaId) => {
      const eid = current[field] || suggested[field] || null;
      const isSug = !current[field] && !!suggested[field];
      const v = info(eid, isSug);
      setTxt(nameId, v.name);
      setTxt(metaId, v.meta);

      // Update button label (Select… vs Change…)
      try{
        const btn = document.querySelector(`button[data-pick="${field}"]`);
        if (btn) btn.textContent = current[field] ? 'Change…' : 'Select…';
      } catch(e){}
    };

    show('soc','cfgSocName','cfgSocMeta');
    show('voltage','cfgVoltageName','cfgVoltageMeta');
    show('solar','cfgSolarName','cfgSolarMeta');
    show('load','cfgLoadName','cfgLoadMeta');

    // Stash for Confirm all
    window.__clawdbotSuggestedMapping = suggested;

    // Confirm-all button UX: show only if at least one is unmapped.
    try{
      const allMapped = !!(current.soc && current.voltage && current.solar && current.load);
      const btn = document.getElementById('btnConfirmAll');
      const res = document.getElementById('confirmAllResult');
      if (btn) {
        btn.style.display = allMapped ? 'none' : '';
      }
      if (res && allMapped) {
        res.textContent = '';
      }
    } catch(e){}
  }

  async function saveMapping(mapping){
    await callService('clawdbot','set_mapping',{mapping});
    setConfigMapping(mapping);
    fillMappingInputs();
    try{ await refreshEntities(); } catch(e){}
  }

  function toast(msg){
    try{
      const el = document.getElementById('toast');
      if (!el) return;
      el.textContent = String(msg || '');
      el.classList.remove('hidden');
      clearTimeout(window.__clawdbotToastTimer);
      window.__clawdbotToastTimer = setTimeout(() => { try{ el.classList.add('hidden'); }catch(e){} }, 2200);
    } catch(e){}
  }

  // ---------------- Setup: dynamic options registry ----------------

  function _optStr(v){
    if (v == null) return '';
    return String(v);
  }

  async function refreshSetupOptions(){
    const root = document.getElementById('setupOptions');
    if (!root) return;
    root.textContent = 'Loading…';

    let options = [];
    try{
      const resp = await callServiceResponse('clawdbot','setup_options_list',{});
      const data = (resp && resp.response) ? resp.response : resp;
      const r = data && data.result ? data.result : data;
      options = (r && Array.isArray(r.options)) ? r.options : [];
    } catch(e){
      root.textContent = 'Failed to load setup options.';
      return;
    }

    if (!options.length) {
      root.textContent = 'No setup options defined.';
      return;
    }

    // Find active env (best-effort)
    let activeEnv = null;
    try{
      const te = options.find(o => o && o.key === 'clawdbot.target_env');
      if (te && te.value != null) activeEnv = String(te.value);
      else if (te && te.default != null) activeEnv = String(te.default);
    } catch(e){}

    root.innerHTML = '';

    const wrap = document.createElement('div');
    wrap.style.display = 'flex';
    wrap.style.flexDirection = 'column';
    wrap.style.gap = '10px';

    for (const opt of options){
      if (!opt || !opt.key) continue;
      const key = String(opt.key);
      const typ = opt.type ? String(opt.type) : 'string';
      const label = opt.label ? String(opt.label) : key;
      const desc = opt.description ? String(opt.description) : '';
      const env = opt.env ? String(opt.env) : null;
      const masked = !!opt.masked || typ === 'secret';
      const readOnly = !!opt.readOnly;
      const validation = (opt.validation && typeof opt.validation === 'object') ? opt.validation : {};
      const allowed = Array.isArray(validation.allowed) ? validation.allowed.map(String) : null;

      const row = document.createElement('div');
      row.style.border = '1px solid var(--divider-color)';
      row.style.borderRadius = '14px';
      row.style.padding = '10px 12px';
      row.style.background = 'color-mix(in srgb, var(--ha-card-background, var(--card-background-color)) 92%, transparent)';

      if (env && activeEnv && env !== activeEnv){
        row.style.opacity = '0.65';
      }

      const head = document.createElement('div');
      head.style.display = 'flex';
      head.style.justifyContent = 'space-between';
      head.style.gap = '10px';

      const left = document.createElement('div');
      left.style.minWidth = '0';

      const t = document.createElement('div');
      t.style.fontWeight = '800';
      t.style.overflow = 'hidden';
      t.style.textOverflow = 'ellipsis';
      t.style.whiteSpace = 'nowrap';
      t.textContent = label;

      const meta = document.createElement('div');
      meta.className = 'muted';
      meta.style.fontSize = '11px';
      meta.textContent = `${key}${env ? ` · env:${env}` : ''}${readOnly ? ' · read-only' : ''}`;

      left.appendChild(t);
      left.appendChild(meta);

      const right = document.createElement('div');
      right.className = 'muted';
      right.style.fontSize = '11px';
      right.style.whiteSpace = 'nowrap';
      right.textContent = typ;

      head.appendChild(left);
      head.appendChild(right);

      const body = document.createElement('div');
      body.style.marginTop = '8px';

      if (desc){
        const d = document.createElement('div');
        d.className = 'muted';
        d.style.marginBottom = '8px';
        d.textContent = desc;
        body.appendChild(d);
      }

      const controls = document.createElement('div');
      controls.style.display = 'flex';
      controls.style.flexWrap = 'wrap';
      controls.style.alignItems = 'center';
      controls.style.gap = '8px';

      let input = null;
      if (typ === 'bool') {
        input = document.createElement('input');
        input.type = 'checkbox';
        input.checked = !!opt.value || (opt.value == null && !!opt.default);
      } else if (typ === 'select' && allowed) {
        input = document.createElement('select');
        input.className = 'select';
        for (const a of allowed) {
          const o = document.createElement('option');
          o.value = a;
          o.textContent = a;
          input.appendChild(o);
        }
        const cur = (opt.value != null) ? String(opt.value) : (opt.default != null ? String(opt.default) : '');
        if (cur) input.value = cur;
      } else {
        input = document.createElement('input');
        input.style.flex = '1';
        input.style.minWidth = '260px';
        input.placeholder = opt.placeholder ? String(opt.placeholder) : (masked ? '********' : '');
        input.value = masked ? '' : _optStr(opt.value != null ? opt.value : (opt.default != null ? opt.default : ''));
        if (masked) input.type = 'password';
      }
      // stable selectors for automation
      try{ input.setAttribute('data-testid', `setup-opt-input:${key}`); }catch(e){}
      try{ input.setAttribute('data-opt-key', key); }catch(e){}
      if (readOnly) input.disabled = true;

      const btn = document.createElement('button');
      btn.className = 'btn primary';
      btn.textContent = 'Save';
      btn.disabled = readOnly;
      try{ btn.setAttribute('data-testid', `setup-opt-save:${key}`); }catch(e){}
      try{ btn.setAttribute('data-opt-key', key); }catch(e){}

      const res = document.createElement('span');
      res.className = 'muted';
      res.style.fontSize = '12px';

      btn.onclick = async () => {
        try{
          btn.disabled = true;
          res.textContent = 'Saving…';
          let val = null;
          if (typ === 'bool') val = !!input.checked;
          else if (typ === 'select') val = String(input.value || '');
          else val = String(input.value || '');

          // secret blank => NOOP (backend also enforces)
          const payload = { key, value: val, source: 'ui' };
          const rr = await callServiceResponse('clawdbot','setup_option_set', payload);
          const dd = (rr && rr.response) ? rr.response : rr;
          const rrr = dd && dd.result ? dd.result : dd;
          if (rrr && rrr.noop) {
            res.textContent = 'No change.';
            toast(`${label}: unchanged`);
          } else {
            res.textContent = 'Saved.';
            toast(`${label}: saved`);
          }
          // refresh list so meta/target_env highlight updates
          await refreshSetupOptions();
        } catch(e){
          const msg = String(e && (e.message || e) || e);
          res.textContent = 'Error.';
          toast(`${label}: save failed (${msg})`);
        } finally {
          btn.disabled = readOnly;
        }
      };

      controls.appendChild(input);
      controls.appendChild(btn);
      controls.appendChild(res);

      body.appendChild(controls);

      row.appendChild(head);
      row.appendChild(body);
      wrap.appendChild(row);
    }

    root.appendChild(wrap);
  }

  // ---------------- Agent view (high-tech profile + STT) ----------------

  function fmtDur(ms){
    try{
      const s = Math.max(0, Math.floor(ms/1000));
      const h = Math.floor(s/3600);
      const m = Math.floor((s%3600)/60);
      const ss = s%60;
      if (h>0) return `${h}h ${m}m`;
      if (m>0) return `${m}m ${ss}s`;
      return `${ss}s`;
    } catch(e){ return '—'; }
  }

  let _agentStartMs = Date.now();
  let _agentUptimeTimer = null;
  let _agentActivity = [];
  let _speechRec = null;
  let _speechActive = false;

  async function refreshAgentJournal(){
    const el = document.getElementById('agentJournal');
    if (!el) return;
    try{
      const resp = await callServiceResponse('clawdbot','journal_list', { limit: 50 });
      const data = (resp && resp.response) ? resp.response : resp;
      const r = data && data.result ? data.result : data;
      const items = (r && Array.isArray(r.items)) ? r.items : [];
      if (!items.length) { el.textContent = 'No journal entries yet.'; return; }

      // Pagination (10 per page)
      const pageSize = 10;
      const total = items.length;
      const totalPages = Math.max(1, Math.ceil(total / pageSize));
      let page = window.__CLAWDBOT_JOURNAL_PAGE || 1;
      if (page < 1) page = 1;
      if (page > totalPages) page = totalPages;
      window.__CLAWDBOT_JOURNAL_PAGE = page;

      el.innerHTML = '';

      const topBar = document.createElement('div');
      topBar.style.display = 'flex';
      topBar.style.justifyContent = 'space-between';
      topBar.style.alignItems = 'center';
      topBar.style.gap = '10px';
      topBar.style.margin = '6px 0 10px 0';

      const left = document.createElement('div');
      left.className = 'muted';
      left.textContent = `Showing ${Math.min(pageSize, total - (page-1)*pageSize)} / ${total}`;

      const right = document.createElement('div');
      right.className = 'muted';
      right.style.display = 'flex';
      right.style.alignItems = 'center';
      right.style.gap = '6px';

      const btnPrev = document.createElement('button');
      btnPrev.className = 'btn';
      btnPrev.style.height = '34px';
      btnPrev.style.padding = '0 10px';
      btnPrev.textContent = '‹';
      btnPrev.disabled = page <= 1;
      btnPrev.onclick = async () => { window.__CLAWDBOT_JOURNAL_PAGE = Math.max(1, page-1); await refreshAgentJournal(); };

      const pageText = document.createElement('span');
      pageText.textContent = `Page ${page}/${totalPages}`;

      const btnNext = document.createElement('button');
      btnNext.className = 'btn';
      btnNext.style.height = '34px';
      btnNext.style.padding = '0 10px';
      btnNext.textContent = '›';
      btnNext.disabled = page >= totalPages;
      btnNext.onclick = async () => { window.__CLAWDBOT_JOURNAL_PAGE = Math.min(totalPages, page+1); await refreshAgentJournal(); };

      right.appendChild(btnPrev);
      right.appendChild(pageText);
      right.appendChild(btnNext);

      topBar.appendChild(left);
      topBar.appendChild(right);
      el.appendChild(topBar);

      // Newest-first so "latest journal" is visually the top entry
      const ordered = items.slice().reverse();
      const start = (page - 1) * pageSize;
      const pageItems = ordered.slice(start, start + pageSize);

      const renderInline = (txt) => {
        // Safe: escape everything, then format inline `code`
        const s = String(txt || '');
        const parts = s.split('`');
        let out = '';
        for (let i=0;i<parts.length;i++){
          const p = escapeHtml(parts[i]);
          if (i % 2 === 1) out += `<code>${p}</code>`; else out += p;
        }
        return out;
      };

      const renderBody = (txt) => {
        const s = String(txt || '');
        // handle fenced code blocks ```...```
        if (s.includes('```')) {
          const chunks = s.split('```');
          let html = '';
          for (let i=0;i<chunks.length;i++){
            const c = chunks[i];
            if (i % 2 === 1) {
              html += `<pre style="margin:10px 0 0 0;padding:10px 12px;border-radius:12px;border:1px solid var(--cb-border);background:color-mix(in srgb, var(--primary-background-color) 70%, var(--cb-card-bg));overflow:auto"><code>${escapeHtml(c.trim())}</code></pre>`;
            } else {
              html += `<div class="muted" style="white-space:pre-wrap">${renderInline(c)}</div>`;
            }
          }
          return html;
        }
        return `<div class="muted" style="white-space:pre-wrap">${renderInline(s)}</div>`;
      };

      for (const it of pageItems){

        const row = document.createElement('div');
        row.style.border = '1px solid var(--divider-color)';
        row.style.borderRadius = '14px';
        row.style.padding = '10px 12px';
        row.style.margin = '10px 0';
        row.style.background = 'linear-gradient(120deg, color-mix(in srgb, var(--ha-card-background, var(--card-background-color)) 92%, transparent), color-mix(in srgb, var(--claw-bg-2) 12%, transparent))';
        const ts = it.ts ? String(it.ts) : '';
        const mood = it.mood ? String(it.mood) : '';
        const title = it.title ? String(it.title) : 'Journal';
        const body = it.body ? String(it.body) : '';
        row.innerHTML = `<div style="display:flex;justify-content:space-between;gap:10px"><div style="font-weight:800">${escapeHtml(title)}${mood ? ` <span class=\"muted\">(${escapeHtml(mood)})</span>` : ''}</div><div class="muted" style="font-size:11px;white-space:nowrap">${escapeHtml(ts.slice(0,19).replace('T',' '))}</div></div><div style="margin-top:6px">${renderBody(body)}</div>`;
        el.appendChild(row);
      }
    } catch(e){
      el.textContent = 'Failed to load journal.';
    }
  }

  function agentAddActivity(kind, text){
    const now = new Date();
    _agentActivity.unshift({ ts: now.toISOString(), kind, text: String(text||'') });
    _agentActivity = _agentActivity.slice(0,5);
    const el = document.getElementById('agentActivity');
    if (!el) return;
    if (!_agentActivity.length) { el.textContent = 'No activity yet.'; return; }
    el.innerHTML = '';
    for (const it of _agentActivity){
      const row = document.createElement('div');
      row.style.border = '1px solid var(--divider-color)';
      row.style.borderRadius = '14px';
      row.style.padding = '10px 12px';
      row.style.margin = '10px 0';
      row.style.background = 'linear-gradient(120deg, color-mix(in srgb, var(--ha-card-background, var(--card-background-color)) 92%, transparent), color-mix(in srgb, #00f5ff 6%, transparent))';
      row.innerHTML = `<div style="display:flex;justify-content:space-between;gap:10px"><div style="font-weight:700;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(it.kind)}</div><div class="muted" style="font-size:11px;white-space:nowrap">${escapeHtml(it.ts.slice(11,19))}</div></div><div class="muted" style="margin-top:4px;white-space:pre-wrap">${escapeHtml(it.text)}</div>`;
      el.appendChild(row);
    }
  }

  let _agentAutoRefreshTimer = null;
  let _agentStateSubUnsub = null;
  let _agentRefreshInFlight = false;
  let _agentLastRefreshMs = 0;
  let _agentLastEventMs = 0;
  let _agentEventCount = 0;

  async function refreshAgentState(){
    try{
      const resp = await callServiceResponse('clawdbot','agent_state_get', {});
      const data = (resp && resp.response) ? resp.response : resp;
      const r = data && data.result ? data.result : data;
      const prof = r && r.profile ? r.profile : null;
      if (prof) {
        window.__CLAWDBOT_CONFIG__.agent_profile = prof;
        const moodEl = document.getElementById('agentMood');
        const descEl = document.getElementById('agentDesc');
        const metaEl = document.getElementById('agentMeta');
        const liveEl = document.getElementById('agentLiveMeta');
        if (moodEl) {
          const mood = prof.mood || 'calm';
          moodEl.textContent = `· mood: ${mood}`;
          try{
            moodEl.classList.remove('mood-calm','mood-alert','mood-focused','mood-degraded','mood-lost','mood-playful','mood-tired');
            moodEl.classList.add('mood-' + mood);
          } catch(e){}
        }
        if (descEl) descEl.textContent = prof.description || '—';
        if (metaEl) {
          const src = prof.source ? String(prof.source) : '—';
          const ts = prof.updated_ts ? String(prof.updated_ts) : '—';
          metaEl.textContent = `source: ${src} · updated: ${ts}`;
        }
        if (liveEl) {
          const lr = _agentLastRefreshMs ? new Date(_agentLastRefreshMs).toISOString().slice(11,19) : '—';
          const le = _agentLastEventMs ? new Date(_agentLastEventMs).toISOString().slice(11,19) : '—';
          liveEl.textContent = `live: event=${_agentEventCount} (last ${le}) · refresh ${lr} · poll 15s`;
        }
        try{
          const hero = document.getElementById('agentHeroCard');
          const mood = prof.mood ? String(prof.mood) : 'calm';
          if (hero) {
            hero.classList.remove('mood-calm','mood-alert','mood-focused','mood-degraded','mood-lost','mood-playful','mood-tired');
            hero.classList.add('mood-' + mood);
          }
        } catch(e){}
      }
    } catch(e){}
  }

  async function renderAgentView(){
    // Uptime ticker
    const uptimeEl = document.getElementById('agentUptime');
    if (_agentUptimeTimer) { clearInterval(_agentUptimeTimer); _agentUptimeTimer=null; }
    _agentUptimeTimer = setInterval(() => {
      try{ if (uptimeEl) uptimeEl.textContent = 'uptime: ' + fmtDur(Date.now() - _agentStartMs); }catch(e){}
    }, 1000);

    const cfg = (window.__CLAWDBOT_CONFIG__ || {});
    const sess = cfg.session_key || 'main';
    const sessPill = document.getElementById('agentSessionPill');
    if (sessPill) { sessPill.textContent = 'session: ' + sess; }

    // Agent profile (mood + description)
    try{ await refreshAgentState(); } catch(e){}

    // Derived sensors status
    const derivedPill = document.getElementById('agentDerivedPill');
    let derivedOn = null;
    try{
      const r = await callServiceResponse('clawdbot','derived_sensors_status',{});
      const data = (r && r.response) ? r.response : r;
      const rr = data && data.result ? data.result : data;
      derivedOn = !!(rr && rr.enabled);
      if (derivedPill) {
        derivedPill.textContent = derivedOn ? 'virtual sensors: ON' : 'virtual sensors: OFF';
        derivedPill.title = 'Creates extra helper sensors (net power, load avg, etc.)';
        derivedPill.classList.toggle('ok', derivedOn);
        derivedPill.classList.toggle('bad', !derivedOn);
      }
    } catch(e){
      if (derivedPill) { derivedPill.textContent = 'derived: —'; derivedPill.classList.remove('ok'); derivedPill.classList.remove('bad'); }
    }

    // Gateway health (latency)
    const connPill = document.getElementById('agentConnPill');
    let gatewayOk = null;
    try{
      const r = await callServiceResponse('clawdbot','gateway_test',{});
      const data = (r && r.response) ? r.response : r;
      const rr = data && data.result ? data.result : data;
      const ms = rr && rr.latency_ms != null ? Number(rr.latency_ms) : null;
      gatewayOk = true;
      if (connPill) {
        connPill.textContent = ms != null && !Number.isNaN(ms) ? `gateway OK (${ms}ms)` : 'gateway OK';
        connPill.classList.add('ok');
        connPill.classList.remove('bad');
      }
    } catch(e){
      gatewayOk = false;
      if (connPill) {
        connPill.textContent = 'gateway FAIL';
        connPill.classList.add('bad');
        connPill.classList.remove('ok');
      }
    }

    // Mood: use the agent-managed profile mood (do NOT override with local heuristics)
    let mood = null;
    try{
      const prof = (window.__CLAWDBOT_CONFIG__ || {}).agent_profile || {};
      if (prof && prof.mood) mood = String(prof.mood);
    } catch(e){}
    if (!mood) mood = 'calm';

    const moodEl = document.getElementById('agentMood');
    if (moodEl) moodEl.textContent = `· mood: ${mood}`;

    // Apply mood styling to hero card
    try{
      const hero = document.getElementById('agentHeroCard');
      if (hero) {
        hero.classList.remove('mood-calm','mood-alert','mood-focused','mood-degraded','mood-lost','mood-playful','mood-tired');
        hero.classList.add('mood-' + mood);
      }
    } catch(e){}

    // Auto theme on mood changes (if enabled)
    try{
      const cfg2 = (window.__CLAWDBOT_CONFIG__ || {});
      if (cfg2.theme && cfg2.theme.auto) {
        const next = (mood === 'alert') ? 'crimson_night' : (mood === 'focused' ? 'deep_ocean' : 'aurora');
        if (cfg2.theme.preset !== next) {
          cfg2.theme.preset = next;
          applyThemePreset(next, {silent:false, mood});
          // persist quietly
          try{ await callServiceResponse('clawdbot','theme_set',{preset: next, auto:true}); } catch(e){}
        }
      }
    } catch(e){}

    // Journal
    try{ await refreshAgentJournal(); } catch(e){}

    agentAddActivity('status', 'Agent view refreshed');

    // Live refresh: subscribe to HA event; fallback poll while Agent tab is visible.
    const refreshNow = async () => {
      try{
        const view = document.getElementById('viewAgent');
        if (view && view.classList && view.classList.contains('hidden')) return;
      } catch(e){}

      const now = Date.now();
      if (_agentRefreshInFlight) return;
      if (now - _agentLastRefreshMs < 1000) return; // debounce
      _agentRefreshInFlight = true;
      try{
        await refreshAgentState();
        await refreshAgentJournal();
        _agentLastRefreshMs = Date.now();
        try{
          const liveEl = document.getElementById('agentLiveMeta');
          if (liveEl) {
            const lr = new Date(_agentLastRefreshMs).toISOString().slice(11,19);
            const le = _agentLastEventMs ? new Date(_agentLastEventMs).toISOString().slice(11,19) : '—';
            liveEl.textContent = `live: event=${_agentEventCount} (last ${le}) · refresh ${lr} · poll 15s`;
          }
        } catch(e){}

      } catch(e){} finally {
        _agentRefreshInFlight = false;
      }
    };

    try{
      if (_agentAutoRefreshTimer) { clearInterval(_agentAutoRefreshTimer); _agentAutoRefreshTimer=null; }
      _agentAutoRefreshTimer = setInterval(refreshNow, 15000);
    } catch(e){}

    try{
      if (!_agentStateSubUnsub) {
        const { conn } = await getHass();
        if (conn && conn.subscribeEvents) {
          _agentStateSubUnsub = await conn.subscribeEvents(async (_ev) => {
            try{
              _agentEventCount += 1;
              _agentLastEventMs = Date.now();
            } catch(e){}
            try{ await refreshNow(); }catch(e){}
          }, 'clawdbot_agent_state_changed');
        }
      }
    } catch(e){}

    bindSpeechUi();
  }

  function bindSpeechUi(){
    const btn = document.getElementById('btnListen');
    const btnStop = document.getElementById('btnStopListen');
    const statusEl = document.getElementById('listenStatus');
    const outEl = document.getElementById('transcript');
    if (!btn || !btnStop || !statusEl || !outEl) return;

    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      statusEl.textContent = 'SpeechRecognition not supported in this browser.';
      btn.disabled = true;
      btnStop.disabled = true;
      return;
    }

    if (!_speechRec) {
      _speechRec = new SpeechRecognition();
      _speechRec.lang = 'en-US';
      _speechRec.interimResults = true;
      _speechRec.continuous = true;

      _speechRec.onresult = (ev) => {
        try{
          let full = '';
          for (let i = ev.resultIndex; i < ev.results.length; i++) {
            const r = ev.results[i];
            full += r[0] && r[0].transcript ? r[0].transcript : '';
          }
          if (full) outEl.textContent = full.trim();
        } catch(e){}
      };
      _speechRec.onerror = (ev) => {
        statusEl.textContent = 'mic error: ' + String(ev && (ev.error || ev.message) || ev);
        _speechActive = false;
        btn.disabled = false;
        btnStop.disabled = true;
      };
      _speechRec.onend = () => {
        if (_speechActive) {
          // stopped unexpectedly
          _speechActive = false;
          btn.disabled = false;
          btnStop.disabled = true;
          statusEl.textContent = 'stopped';
        }
      };
    }

    if (!btn.__bound) {
      btn.__bound = true;
      btn.onclick = () => {
        try{
          outEl.textContent = '';
          statusEl.textContent = 'listening…';
          _speechActive = true;
          btn.disabled = true;
          btnStop.disabled = false;
          _speechRec.start();
          agentAddActivity('voice', 'Listening started');
        } catch(e){
          statusEl.textContent = 'failed to start: ' + String(e);
          _speechActive = false;
          btn.disabled = false;
          btnStop.disabled = true;
        }
      };
    }
    if (!btnStop.__bound) {
      btnStop.__bound = true;
      btnStop.onclick = () => {
        try{
          _speechActive = false;
          _speechRec.stop();
          statusEl.textContent = 'stopped';
          btn.disabled = false;
          btnStop.disabled = true;
          agentAddActivity('voice', 'Listening stopped');
        } catch(e){}
      };
    }
  }

  async function setMappingField(field, entityId){
    const mapping = mappingWithDefaults();
    mapping[field] = entityId || null;
    await saveMapping(mapping);
    toast(`Saved ${field} → ${entityId || 'cleared'}`);
  }

  function bindEntityConfigUi(){
    // Select buttons
    for (const btn of document.querySelectorAll('button[data-pick]')){
      btn.onclick = () => openPicker(btn.getAttribute('data-pick'));
    }
    for (const btn of document.querySelectorAll('button[data-clear]')){
      btn.onclick = async () => {
        const key = btn.getAttribute('data-clear');
        try{ await setMappingField(key, null); } catch(e){}
      };
    }

    const confirmAll = document.getElementById('btnConfirmAll');
    if (confirmAll) confirmAll.onclick = async () => {
      const res = document.getElementById('confirmAllResult');
      if (res) res.textContent = 'saving…';
      confirmAll.disabled = true;
      try{
        const hass = window.__clawdbotHass || null;
        const suggested = window.__clawdbotSuggestedMapping || computeAutoFill(hass);
        await saveMapping(suggested);
        if (res) res.textContent = 'saved';
        toast('Saved entity configuration');
      } catch(e){
        if (res) res.textContent = 'error';
      } finally {
        confirmAll.disabled = false;
      }
    };

    const advBtn = document.getElementById('btnMapSaveAdvanced');
    if (advBtn) advBtn.onclick = async () => {
      const res = document.getElementById('mapSaveAdvancedResult');
      if (res) res.textContent = 'saving…';
      try{
        const mapping = {
          soc: (document.getElementById('mapSoc')?.value || '').trim() || null,
          voltage: (document.getElementById('mapVoltage')?.value || '').trim() || null,
          solar: (document.getElementById('mapSolar')?.value || '').trim() || null,
          load: (document.getElementById('mapLoad')?.value || '').trim() || null,
        };
        await saveMapping(mapping);
        if (res) res.textContent = 'saved';
        toast('Saved advanced mapping');
      } catch(e){
        if (res) res.textContent = 'error';
      }
    };
  }

  // Simple picker modal (type-to-search). Avoids rendering thousands of entities.
  let _pickerField = null;
  function openPicker(field){
    _pickerField = field;
    const modal = document.getElementById('pickerModal');
    const title = document.getElementById('pickerTitle');
    const search = document.getElementById('pickerSearch');
    const hint = document.getElementById('pickerHint');
    if (!modal || !search) return;
    if (title) title.textContent = `Select entity for ${field}`;
    if (hint) hint.textContent = 'Type to search. Showing top suggestions first.';
    modal.classList.remove('hidden');
    search.value = '';
    renderPickerList('');
    setTimeout(() => { try{ search.focus(); }catch(e){} }, 0);
  }
  function closePicker(){
    const modal = document.getElementById('pickerModal');
    if (modal) modal.classList.add('hidden');
    _pickerField = null;
  }
  function bindPickerModal(){
    const closeBtn = document.getElementById('pickerClose');
    if (closeBtn) closeBtn.onclick = closePicker;
    const modal = document.getElementById('pickerModal');
    if (modal) modal.onclick = (e) => { if (e.target === modal) closePicker(); };
    const search = document.getElementById('pickerSearch');
    if (search) search.oninput = () => renderPickerList(search.value || '');
  }

  function pickerRules(field){
    const rules={
      soc: { label:'Battery SOC (%)', keywords:['soc','state_of_charge','battery_soc','clawdbot_test_battery_soc'], units:['%'], weak:['battery'] },
      voltage: { label:'Battery Voltage (V)', keywords:['voltage','battery_voltage','batt_v','clawdbot_test_battery_voltage'], units:['v'], weak:['battery'] },
      solar: { label:'Solar Power (W)', keywords:['solar','pv','photovoltaic','panel','clawdbot_test_solar_w'], units:['w'], weak:['power','input'] },
      load: { label:'Load Power (W)', keywords:['load','consumption','house_power','ac_load','power','clawdbot_test_load_w'], units:['w'], weak:['total','sum'] },
    };
    return rules[field] || rules.soc;
  }

  function bestCandidate(field, hass){
    const rules = pickerRules(field);
    const states = hass && hass.states ? hass.states : {};
    let best = null;
    let bestScore = -999;
    for (const [entity_id, st] of Object.entries(states)){
      const meta={
        entity_id,
        name: (st && st.attributes && (st.attributes.friendly_name || st.attributes.device_class || '')) || '',
        unit: (st && st.attributes && st.attributes.unit_of_measurement) || '',
        state: st ? st.state : '',
      };
      const s = scoreEntity(meta, rules);
      if (s > bestScore) { bestScore = s; best = meta; best.score = s; }
    }
    if (!best || bestScore <= 0) return null;
    return best;
  }

  function computeAutoFill(hass){
    const m = mappingWithDefaults();
    const out = { ...m };
    for (const k of ['soc','voltage','solar','load']){
      if (!out[k]) {
        const b = bestCandidate(k, hass);
        if (b && b.entity_id) out[k] = b.entity_id;
      }
    }
    return out;
  }

  function renderPickerList(query){
    const listEl = document.getElementById('pickerList');
    if (!listEl) return;
    listEl.innerHTML = '';
    const field = _pickerField;
    if (!field) return;

    // Use latest hydrated hass if present
    const hass = window.__clawdbotHass || null;
    const states = hass && hass.states ? hass.states : {};
    const q = String(query||'').trim().toLowerCase();
    const rules = pickerRules(field);

    // Candidate pool: _allIds limited and filtered by query (if any)
    let ids = _allIds || [];
    if (q) ids = ids.filter(id => id.toLowerCase().includes(q));

    // Score and take top 50
    const scored=[];
    for (const id of ids){
      const st = states[id];
      const meta={
        entity_id: id,
        name: (st && st.attributes && (st.attributes.friendly_name||'')) || '',
        unit: (st && st.attributes && st.attributes.unit_of_measurement) || '',
        state: st ? st.state : '',
      };
      const s = scoreEntity(meta, rules) + (q ? 0 : 0); // base
      if (q && !s) {
        // still allow direct matches
        scored.push({score: 0, ...meta});
      } else {
        scored.push({score: s, ...meta});
      }
    }
    scored.sort((a,b)=>b.score-a.score);
    const top = scored.slice(0, 50);

    for (const it of top){
      const row = document.createElement('div');
      row.className = 'pick-item';
      const main = document.createElement('div');
      main.className = 'pick-main';
      const name = document.createElement('div');
      name.className = 'pick-name';
      const fname = it.name ? String(it.name) : it.entity_id;
      name.textContent = fname;
      const meta = document.createElement('div');
      meta.className = 'pick-meta';
      const unit = it.unit ? (' '+it.unit) : '';
      meta.textContent = `${it.entity_id} · ${it.state}${unit}`;
      main.appendChild(name); main.appendChild(meta);
      const btn = document.createElement('button');
      btn.className = 'btn primary';
      btn.textContent = 'Use';
      btn.onclick = async (ev) => {
        ev.preventDefault(); ev.stopPropagation();
        try{ await setMappingField(field, it.entity_id); } catch(e){}
        closePicker();
      };
      row.appendChild(main);
      row.appendChild(btn);
      listEl.appendChild(row);
    }

    if (!top.length){
      const empty = document.createElement('div');
      empty.className = 'muted';
      empty.textContent = 'No matches.';
      listEl.appendChild(empty);
    }
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

  async function refreshSuggestedSensors(){
    const listEl = document.getElementById('suggestedSensorsList');
    const statusEl = document.getElementById('derivedStatus');
    if (!listEl) return;

    const parseResp = (resp) => {
      const data = (resp && resp.response) ? resp.response : resp;
      return data && data.result ? data.result : data;
    };

    let enabled = false;
    try{
      const st = parseResp(await callServiceResponse('clawdbot','derived_sensors_status',{}));
      enabled = !!(st && st.enabled);
      if (statusEl) statusEl.textContent = enabled ? 'Active (updates every ~10s)' : 'Not created';
    } catch(e){
      if (statusEl) statusEl.textContent = '';
    }

    try{
      const r = parseResp(await callServiceResponse('clawdbot','derived_sensors_suggest',{}));
      const suggestions = (r && Array.isArray(r.suggestions)) ? r.suggestions : [];
      listEl.innerHTML = '';

      if (!suggestions.length) {
        listEl.innerHTML = '<div class="muted">No suggestions yet (map solar/load first).</div>';
        return;
      }

      for (const s of suggestions){
        const pv = s && s.preview ? s.preview : null;
        const attrs = pv && pv.attributes ? pv.attributes : {};
        const name = (attrs && attrs.friendly_name) ? String(attrs.friendly_name) : (s.entity_id || '—');
        const unit = (attrs && attrs.unit_of_measurement) ? String(attrs.unit_of_measurement) : '';
        const val = pv && pv.state != null ? String(pv.state) : '—';
        const uses = Array.isArray(s.uses) ? s.uses.filter(Boolean).slice(0,3) : [];
        const why = s.why ? String(s.why) : '';

        const row = document.createElement('div');
        row.style.border = '1px solid var(--divider-color)';
        row.style.borderRadius = '12px';
        row.style.padding = '10px 12px';
        row.style.margin = '8px 0';

        const right = enabled
          ? '<span class="pill" style="background: color-mix(in srgb, var(--primary-color) 15%, transparent);">Active</span>'
          : '<button class="btn primary" data-derive-create="1" style="white-space:nowrap">Create</button>';

        row.innerHTML = `
          <div class="row" style="justify-content:space-between;align-items:flex-start;gap:10px">
            <div style="min-width:0;flex:1">
              <div style="font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escapeHtml(name)}</div>
              <div class="muted" style="margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escapeHtml(s.entity_id || '')} · ${escapeHtml(val)}${unit ? ' '+escapeHtml(unit) : ''}</div>
              <div class="muted" style="margin-top:4px">Why: ${escapeHtml(why)}</div>
              <div class="muted" style="margin-top:4px">Uses: ${escapeHtml(uses.join(', ') || '—')}</div>
            </div>
            <div>${right}</div>
          </div>
        `;

        if (!enabled) {
          const btn = row.querySelector('button[data-derive-create]');
          if (btn) btn.onclick = async () => {
            btn.disabled = true;
            try{
              await callServiceResponse('clawdbot','derived_sensors_set_enabled',{enabled:true});
              toast('Virtual sensors enabled');
              await refreshSuggestedSensors();
            } catch(e){
              toast('Create failed: ' + String(e));
            } finally {
              btn.disabled = false;
            }
          };
        }

        listEl.appendChild(row);
      }
    } catch(e){
      listEl.innerHTML = '<div class="muted">Error loading suggestions: ' + escapeHtml(String(e)) + '</div>';
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
    try{ if (DEBUG_UI) console.debug('[clawdbot] renderMappedValues mapping', m); } catch(e){}

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
          try{
            if (DEBUG_UI) console.debug('[clawdbot] mapped entity missing in hass.states', { key: r.key, entity_id: r.entity_id, statesType: (hass && hass.states && (Array.isArray(hass.states)?'array':typeof hass.states)), statesCount: (hass && hass.states && typeof hass.states==='object') ? Object.keys(hass.states).length : null });
          } catch(e){}
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
            try{ if (DEBUG_UI) console.debug('[clawdbot] getHass via hassConnection', true); }catch(e){}
            return { conn: hc.conn, hass };
          }
          // Stash the conn and keep looking for hass via other paths.
          parent.__clawdbotConn = hc.conn;
        }
      }
    } catch(e) {}

    // Path 2: legacy global hass
    try{
      if (parent.hass && parent.hass.connection) {
        try{ if (DEBUG_UI) console.debug('[clawdbot] getHass via parent.hass', !!(parent.hass && parent.hass.states)); }catch(e){};
        return { conn: parent.hass.connection, hass: parent.hass };
      }
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

    // Hard timeout so panel never silently hangs.
    const WS_TIMEOUT_MS = 3000;

    console.debug('[clawdbot] ws get_states send');
    const list = await Promise.race([
      conn.sendMessagePromise({ type: 'get_states' }),
      new Promise((_, rej) => setTimeout(() => rej(new Error('WS get_states timeout after ' + WS_TIMEOUT_MS + 'ms')), WS_TIMEOUT_MS)),
    ]);
    console.debug('[clawdbot] ws get_states done');

    // HA returns an array of state objects; normalize to a dict keyed by entity_id.
    const out = {};
    if (Array.isArray(list)) {
      for (const it of list) {
        if (it && it.entity_id) out[it.entity_id] = it;
      }
    }

    console.debug('[clawdbot] ws get_states normalized', {listIsArray: Array.isArray(list), listLen: Array.isArray(list)?list.length:null, outLen: Object.keys(out).length});
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

  function getParentHassStates(){
    // Best-effort access to HA frontend state tree (same-origin iframe).
    try{
      const p = window.parent;
      if (!p) return null;
      if (p.hass && p.hass.states) return p.hass.states;
      const doc = p.document;
      const ha = doc && doc.querySelector ? doc.querySelector('home-assistant') : null;
      const main = ha && ha.shadowRoot && ha.shadowRoot.querySelector ? ha.shadowRoot.querySelector('home-assistant-main') : null;
      const hass = (main && (main.hass || main._hass)) || (ha && (ha.hass || ha._hass)) || null;
      return hass && hass.states ? hass.states : null;
    } catch(e){
      return null;
    }
  }

  async function refreshEntities(){
    try{ if (DEBUG_UI) dbgStep('refresh-start');
    console.debug('[clawdbot] refreshEntities start'); }catch(e) {}

    const { hass, conn } = await getHass();

    // Export a handle immediately so we can introspect even if WS hangs.
    try{ window.__clawdbotHass = hass; } catch(e){}

    // Always refresh from websocket (with timeout inside fetchStatesWs).
    let states = null;
    try{
      const preStates = (hass && hass.states) ? hass.states : null;
      const preCount = (preStates && typeof preStates === 'object') ? Object.keys(preStates).length : null;
      console.debug('[clawdbot] refreshEntities pre', { preType: Array.isArray(preStates)?'array':typeof preStates, preCount });
    } catch(e){}

    try{
      console.debug('[clawdbot] refreshEntities WS get_states');
      states = await fetchStatesWs(conn);
    } catch(e) {
      console.debug('[clawdbot] refreshEntities WS failed', String(e && (e.message||e)));
      // Fallback A: parent hass.states (most reliable for embedded UI)
      const parentStates = getParentHassStates();
      if (parentStates && typeof parentStates === 'object') {
        console.debug('[clawdbot] refreshEntities using parent hass.states', {count: Object.keys(parentStates).length});
        states = parentStates;
      } else {
        // Fallback B: REST (may 401 in iframe)
        try{
          states = await fetchStatesRest(hass);
        } catch(e2){
          setStatus(false,'error', String(e2));
          throw e2;
        }
      }
    }

    // Hydrate iframe hass snapshot so Cockpit/mapping reads the same state object.
    try{
      if (hass) {
        if (Array.isArray(states)) {
          const byId = {};
          for (const it of states) { if (it && it.entity_id) byId[it.entity_id] = it; }
          states = byId;
        }
        hass.states = states || {};
        window.__clawdbotStatesType = Array.isArray(hass.states) ? 'array' : (hass.states && typeof hass.states === 'object' ? 'object' : typeof hass.states);
        window.__clawdbotStatesCount = (hass.states && typeof hass.states === 'object') ? Object.keys(hass.states).length : null;
        console.debug('[clawdbot] hydrated hass.states', {type: window.__clawdbotStatesType, count: window.__clawdbotStatesCount});
      }
    } catch(e) {}

    _allIds = Object.keys(states || {}).sort();
    buildMappingDatalist(hass);
    renderEntities(hass, qs('#filter').value);
    try{ renderEntityConfig(hass); } catch(e){}
    try{ renderSuggestions(hass); } catch(e){}
    try{ renderMappedValues(hass); } catch(e){}
    try{ renderRecommendations(hass); } catch(e){}

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
    fillThemeInputs();
    fillMappingInputs();
    renderHouseMemory();
    renderMappedValues(null);
    renderSuggestions(null);
    try{ bindEntityConfigUi(); bindPickerModal(); } catch(e){}

    async function switchTab(which){
      const setupTab = qs('#tabSetup');
      const cockpitTab = qs('#tabCockpit');
      const chatTab = qs('#tabChat');
      const viewSetup = qs('#viewSetup');
      const viewCockpit = qs('#viewCockpit');
      const viewAgent = qs('#viewAgent');
      const viewChat = qs('#viewChat');
      const agentTab = qs('#tabAgent');
      if (!setupTab || !cockpitTab || !chatTab || !agentTab || !viewSetup || !viewCockpit || !viewAgent || !viewChat) return;

      setupTab.classList.toggle('active', which === 'setup');
      cockpitTab.classList.toggle('active', which === 'cockpit');
      agentTab.classList.toggle('active', which === 'agent');
      chatTab.classList.toggle('active', which === 'chat');

      // Hard display toggles (production UI must isolate views)
      setHidden(viewSetup, which !== 'setup');
      setHidden(viewCockpit, which !== 'cockpit');
      setHidden(viewAgent, which !== 'agent');
      setHidden(viewChat, which !== 'chat');

      if (which === 'cockpit') {
    try{ if (DEBUG_UI) dbgStep('before-getHass');
    console.debug('[clawdbot] before getHass'); } catch(e) {}
        try{ const { hass } = await getHass(); await refreshEntities(); renderMappedValues(hass); renderHouseMemory(); renderRecommendations(hass); await refreshSuggestedSensors(); } catch(e){}
      }
      if (which === 'agent') {
        try{ await renderAgentView(); } catch(e){}
      }
      if (which === 'setup') {
        try{ const { hass } = await getHass(); await refreshEntities(); renderEntityConfig(hass); } catch(e){}
        try{ await refreshSetupOptions(); } catch(e){}
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
    bindTab('#tabAgent','agent');
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
        if (id === 'tabAgent') switchTab('agent');
        if (id === 'tabChat') switchTab('chat');
      }, true);
    } catch(e){}

    // Apply theme ASAP (before first render)
    try{ fillThemeInputs(); } catch(e){}

    // Normalize initial state (ensures non-active views are truly hidden).
    switchTab('cockpit');

    qs('#refreshBtn').onclick = refreshEntities;
    qs('#clearFilter').onclick = () => { qs('#filter').value=''; getHass().then(({hass})=>renderEntities(hass,'')); };
    qs('#filter').oninput = async () => { try{ const { hass } = await getHass(); renderEntities(hass, qs('#filter').value); } catch(e){} };

    const btnSave = qs('#btnConnSave');
    if (btnSave) btnSave.onclick = () => saveConnectionOverrides('save');
    const btnReset = qs('#btnConnReset');
    if (btnReset) btnReset.onclick = () => saveConnectionOverrides('reset');

    const btnThemeApply = qs('#btnThemeApply');
    if (btnThemeApply) btnThemeApply.onclick = async () => { try{ await saveTheme(); } catch(e){ toast('Theme save failed: ' + String(e)); } };
    const btnThemeReset = qs('#btnThemeReset');
    if (btnThemeReset) btnThemeReset.onclick = async () => { try{ await resetTheme(); } catch(e){ toast('Theme reset failed: ' + String(e)); } };
    const themeSel = qs('#themePreset');
    if (themeSel) themeSel.onchange = () => { try{ applyThemePreset(themeSel.value, {silent:true}); } catch(e){} };

    const btnDerEnable = qs('#btnDerivedEnable');
    if (btnDerEnable) btnDerEnable.onclick = async () => {
      btnDerEnable.disabled = true;
      try{
        await callServiceResponse('clawdbot','derived_sensors_set_enabled',{enabled:true});
        toast('Virtual sensors enabled');
        await refreshSuggestedSensors();
      } catch(e){
        toast('Enable failed: ' + String(e));
      } finally {
        btnDerEnable.disabled = false;
      }
    };
    const btnDerDisable = qs('#btnDerivedDisable');
    if (btnDerDisable) btnDerDisable.onclick = async () => {
      btnDerDisable.disabled = true;
      try{
        await callServiceResponse('clawdbot','derived_sensors_set_enabled',{enabled:false});
        toast('Virtual sensors disabled');
        await refreshSuggestedSensors();
      } catch(e){
        toast('Disable failed: ' + String(e));
      } finally {
        btnDerDisable.disabled = false;
      }
    };

    qs('#btnGatewayTest').onclick = async () => {
      const el = qs('#gwTestResult');
      if (el) el.textContent = 'running…';
      try{
        const resp = await callServiceResponse('clawdbot','gateway_test',{});
        const data = (resp && resp.response) ? resp.response : resp;
        const r = data && data.result ? data.result : data;
        const ms = r && r.latency_ms != null ? Number(r.latency_ms) : null;
        if (el) el.textContent = 'ok' + (ms != null && !Number.isNaN(ms) ? ` (${ms}ms)` : '');
        toast('Gateway OK' + (ms != null && !Number.isNaN(ms) ? ` (${ms}ms)` : ''));
      } catch(e){
        const msg = String(e && (e.message || e) || e);
        if (el) el.textContent = 'error';
        toast('Gateway FAILED: ' + msg);
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
        const resp = await callServiceResponse('clawdbot','chat_new_session', { label: label || undefined });
        const data = (resp && resp.response) ? resp.response : resp;
        const r = data && data.result ? data.result : data;
        if (r && r.ok === false) {
          try{ console.warn('[clawdbot chat_new_session] debug', r.debug); }catch(e){}
          toast('New session failed: ' + (r.reason || 'unknown error'));
          return;
        }
        const key = r && (r.session_key || r.sessionKey || r.key);
        if (!key) {
          toast('New session failed: no session key returned');
          return;
        }
        await refreshSessions();
        if (sessionSel) {
          sessionSel.value = key;
          chatSessionKey = key;
          await loadChatLatest();
          renderChat({ autoScroll: true });
          await refreshTokenUsage();
          if (chatPollingActive) scheduleChatPoll(CHAT_POLL_INITIAL_MS);
          toast('Created new session');
        }
      } catch(e){
        const msg = String(e && (e.message || e) || e);
        toast('New session failed: ' + msg);
        console.warn('chat_new_session failed', e);
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

      // Append to HA-side chat store (single source of truth). Avoid local push to prevent duplicates.
      try{ await callService('clawdbot','chat_append',{ role:'user', text, session_key: chatSessionKey }); } catch(e){}
      input.value = '';
      try{ await loadChatLatest(); } catch(e){}
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

    // Default landing: Cockpit unless essentials/mapping are missing.
    const cfg = (window.__CLAWDBOT_CONFIG__ || {});
    const firstRun = !!(cfg.essentials_missing || cfg.mapping_missing);
    if (firstRun) {
      qs('#tabSetup').onclick();
      // Lightweight wizard hint banner
      try{ setStatus(true, 'setup needed', 'Complete connection + entity configuration, then return to Cockpit.'); } catch(e){}
    } else {
      qs('#tabCockpit').onclick();
    }

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
    // Idempotent: don't double-init.
    if (window.__clawdbotPanelInit === true) return;

    // Set marker BEFORE any heavy work so we can observe partial boot.
    window.__clawdbotPanelInit = 'booting';
    window.__clawdbotPanelInitError = null;

    const run = async () => {
      try{
        await init();
        window.__clawdbotPanelInit = true;
      } catch(e) {
        window.__clawdbotPanelInit = 'error';
        window.__clawdbotPanelInitError = String(e && (e.stack || e.message || e));
        try{ console.error('[clawdbot] init failed', e); } catch(_e) {}
        // If debug=1, also surface the error in the status UI.
        try{ if (typeof DEBUG_UI !== 'undefined' && DEBUG_UI) setStatus(false,'error', String(e)); } catch(_e) {}
        throw e;
      }
    };

    // Kick once; retry once on next tick in case DOM wasn't ready.
    run().catch(() => {
      try{ setTimeout(() => { run().catch(()=>{}); }, 50); } catch(_e) {}
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', __clawdbotBoot, { once: true });
  } else {
    __clawdbotBoot();
  }
})();
})();
