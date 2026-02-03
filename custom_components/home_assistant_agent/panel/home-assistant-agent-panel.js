class HAAgentPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._entities = [];
    this._suggestions = null;
    this._status = "Loading entities...";
    this._baseUrl = "";
    this._openaiKeyPresent = false;
    this._anthropicKeyPresent = false;
    this._geminiKeyPresent = false;
    this._modelReasoning = "";
    this._modelFast = "";
    this._ttsModel = "";
    this._sttModel = "";
    this._instruction = "";
    this._validation = null;
  }

  _renderModelOptions(selected, options) {
    return options
      .map((model) => {
        const isSelected = model === selected ? "selected" : "";
        return `<option value="${model}" ${isSelected}>${model}</option>`;
      })
      .join("");
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._loaded) {
      this._loaded = true;
      this._loadSettings();
      this._loadEntities();
      this._render();
    }
  }

  _render() {
    if (!this.shadowRoot) {
      return;
    }
    const entityCount = this._entities.length;
    const suggestions = this._suggestions
      ? JSON.stringify(this._suggestions, null, 2)
      : "";

    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          padding: 24px;
          box-sizing: border-box;
          font-family: "IBM Plex Sans", "Fira Sans", "Segoe UI", sans-serif;
          color: var(--primary-text-color);
        }
        .wrap {
          max-width: 900px;
          margin: 0 auto;
          padding: 24px;
          border-radius: 18px;
          background: linear-gradient(135deg, rgba(255, 215, 160, 0.25), rgba(160, 215, 255, 0.2));
          border: 1px solid rgba(0, 0, 0, 0.08);
          box-shadow: 0 12px 30px rgba(0, 0, 0, 0.08);
        }
        h1 {
          font-size: 28px;
          margin: 0 0 8px;
          letter-spacing: 0.4px;
        }
        h2 {
          margin: 18px 0 6px;
          font-size: 18px;
        }
        p {
          margin: 0 0 16px;
          opacity: 0.85;
          line-height: 1.4;
        }
        .subtitle {
          margin: 0 0 12px;
          font-size: 14px;
          opacity: 0.7;
        }
        .row {
          display: flex;
          gap: 12px;
          flex-wrap: wrap;
          align-items: center;
          margin-bottom: 12px;
        }
        input[type="password"] {
          flex: 1;
          min-width: 220px;
          padding: 10px 12px;
          border-radius: 8px;
          border: 1px solid rgba(0, 0, 0, 0.2);
          background: rgba(255, 255, 255, 0.8);
        }
        button {
          padding: 10px 16px;
          border: none;
          border-radius: 8px;
          background: #2f3f5f;
          color: #fdf7e9;
          cursor: pointer;
        }
        select, textarea {
          flex: 1;
          min-width: 220px;
          padding: 10px 12px;
          border-radius: 8px;
          border: 1px solid rgba(0, 0, 0, 0.2);
          background: rgba(255, 255, 255, 0.8);
          font-family: inherit;
        }
        button.secondary {
          background: #c96b3f;
        }
        .status {
          font-size: 14px;
          margin-top: 8px;
          opacity: 0.75;
        }
        pre {
          background: rgba(0, 0, 0, 0.08);
          padding: 12px;
          border-radius: 10px;
          overflow: auto;
          max-height: 300px;
        }
        @media (max-width: 600px) {
          :host {
            padding: 16px;
          }
          .wrap {
            padding: 18px;
          }
          h1 {
            font-size: 22px;
          }
        }
      </style>
      <div class="wrap">
        <h1>Home Assistant Agent</h1>
        <p>Placeholder panel for onboarding. Entities discovered: ${entityCount}</p>
        <h2>Connection</h2>
        <p class="subtitle">Configure the ha_agent_core add-on base URL.</p>
        <div class="row">
          <input id="base-url" type="text" placeholder="http://core-ha_agent_core" value="${this._baseUrl || ""}" />
          <button id="check-addon" class="secondary">Check Add-on</button>
        </div>
        <h2>API Keys</h2>
        <p class="subtitle">Provide one or more keys to unlock model choices.</p>
        <div class="row">
          <input id="openai-key" type="password" placeholder="OpenAI API key" />
          <input id="anthropic-key" type="password" placeholder="Anthropic API key" />
          <input id="gemini-key" type="password" placeholder="Gemini API key" />
        </div>
        <h2>Reasoning Model</h2>
        <p class="subtitle">Choose a powerful model for complex tasks.</p>
        <div class="row">
          <select id="model-reasoning">
            <option value="">Reasoning model (optional)</option>
            ${this._renderModelOptions(this._modelReasoning, [
              "gpt-5.2",
              "gpt-5",
              "claude-opus-4-5",
              "claude-sonnet-4-5",
              "gemini-3-flash-preview",
              "gemini-2.5-flash"
            ])}
          </select>
          <select id="model-fast">
            <option value="">Fast model (optional)</option>
            ${this._renderModelOptions(this._modelFast, [
              "gpt-4.1-mini",
              "claude-haiku-4-5",
              "gemini-2.5-flash-lite"
            ])}
          </select>
        </div>
        <h2>Speech</h2>
        <p class="subtitle">Optional TTS/STT models if you plan to use voice.</p>
        <div class="row">
          <select id="tts-model">
            <option value="">TTS model (optional)</option>
            ${this._renderModelOptions(this._ttsModel, [
              "gpt-5.2-tts",
              "gpt-5-tts",
              "gpt-4o-mini-tts",
              "tts-1",
              "tts-1-hd",
              "claude-opus-4-5-tts",
              "claude-sonnet-4-5-tts",
              "gemini-3-flash-preview-tts",
              "gemini-2.5-flash-preview-tts",
              "gemini-2.5-pro-preview-tts"
            ])}
          </select>
          <select id="stt-model">
            <option value="">STT model (optional)</option>
            ${this._renderModelOptions(this._sttModel, [
              "gpt-4o-transcribe",
              "gpt-4o-mini-transcribe",
              "gpt-4o-transcribe-diarize",
              "whisper-1",
              "chirp_3",
              "claude-opus-4-5-stt",
              "claude-sonnet-4-5-stt",
              "gemini-3-flash-preview-stt",
              "gemini-2.5-flash-stt"
            ])}
          </select>
        </div>
        <h2>Instructions</h2>
        <p class="subtitle">Customize how the agent behaves.</p>
        <div class="row">
          <textarea id="instruction" rows="4" placeholder="Agent instruction">${this._instruction}</textarea>
        </div>
        <div class="row">
          <button id="save-settings">Save Settings</button>
          <button id="run-suggest" class="secondary">Run Suggest</button>
        </div>
        <div class="status">
          OpenAI key: ${this._openaiKeyPresent ? "stored" : "not set"} ·
          Anthropic key: ${this._anthropicKeyPresent ? "stored" : "not set"} ·
          Gemini key: ${this._geminiKeyPresent ? "stored" : "not set"}
        </div>
        ${
          this._validation
            ? `<pre>${JSON.stringify(this._validation, null, 2)}</pre>`
            : ""
        }
        <div class="status">${this._status}</div>
        ${suggestions ? `<pre>${suggestions}</pre>` : ""}
      </div>
    `;

    const input = this.shadowRoot.getElementById("openai-key");
    const stop = (event) => event.stopPropagation();
    input.addEventListener("keydown", stop);
    input.addEventListener("keyup", stop);
    input.addEventListener("keypress", stop);
    input.addEventListener("focusin", stop);
    input.addEventListener("pointerdown", stop);

    const stopIds = [
      "anthropic-key",
      "gemini-key",
      "model-reasoning",
      "model-fast",
      "tts-model",
      "stt-model",
      "instruction"
    ];
    stopIds.forEach((id) => {
      const el = this.shadowRoot.getElementById(id);
      el.addEventListener("keydown", stop);
      el.addEventListener("keyup", stop);
      el.addEventListener("keypress", stop);
      el.addEventListener("focusin", stop);
      el.addEventListener("pointerdown", stop);
    });

    this.shadowRoot.getElementById("save-settings").onclick = () =>
      this._saveSettings();
    this.shadowRoot.getElementById("run-suggest").onclick = () =>
      this._runSuggest();
    this.shadowRoot.getElementById("check-addon").onclick = () =>
      this._checkAddon();
  }

  async _loadSettings() {
    try {
      const data = await this._hass.callApi("GET", "home_assistant_agent/settings");
      this._baseUrl = data.base_url || "";
      this._openaiKeyPresent = Boolean(data.openai_key_present);
      this._anthropicKeyPresent = Boolean(data.anthropic_key_present);
      this._geminiKeyPresent = Boolean(data.gemini_key_present);
      this._modelReasoning = data.model_reasoning || "";
      this._modelFast = data.model_fast || "";
      this._ttsModel = data.tts_model || "";
      this._sttModel = data.stt_model || "";
      this._instruction = data.instruction || "";
      if (!this._baseUrl) {
        this._status = "Add-on base URL not set.";
      }
    } catch (err) {
      this._status = `Failed to load settings: ${err}`;
    }
    this._render();
  }

  async _loadEntities() {
    try {
      const data = await this._hass.callApi(
        "GET",
        "home_assistant_agent/entities"
      );
      this._entities = data.entities || [];
      this._status = `Loaded ${this._entities.length} entities.`;
    } catch (err) {
      this._status = `Failed to load entities: ${err}`;
    }
    this._render();
  }

  async _saveSettings() {
    const baseUrl = this.shadowRoot.getElementById("base-url").value || "";
    const openaiKey = this.shadowRoot.getElementById("openai-key").value || "";
    const anthropicKey = this.shadowRoot.getElementById("anthropic-key").value || "";
    const geminiKey = this.shadowRoot.getElementById("gemini-key").value || "";
    const modelReasoning = this.shadowRoot.getElementById("model-reasoning").value || "";
    const modelFast = this.shadowRoot.getElementById("model-fast").value || "";
    const ttsModel = this.shadowRoot.getElementById("tts-model").value || "";
    const sttModel = this.shadowRoot.getElementById("stt-model").value || "";
    const instruction = this.shadowRoot.getElementById("instruction").value || "";
    try {
      const result = await this._hass.callApi("POST", "home_assistant_agent/settings", {
        base_url: baseUrl,
        openai_key: openaiKey,
        anthropic_key: anthropicKey,
        gemini_key: geminiKey,
        model_reasoning: modelReasoning,
        model_fast: modelFast,
        tts_model: ttsModel,
        stt_model: sttModel,
        instruction,
        validate: true
      });
      this._baseUrl = result.base_url || this._baseUrl;
      this._openaiKeyPresent = Boolean(result.openai_key_present);
      this._anthropicKeyPresent = Boolean(result.anthropic_key_present);
      this._geminiKeyPresent = Boolean(result.gemini_key_present);
      this._modelReasoning = result.model_reasoning || this._modelReasoning;
      this._modelFast = result.model_fast || this._modelFast;
      this._ttsModel = result.tts_model || this._ttsModel;
      this._sttModel = result.stt_model || this._sttModel;
      this._instruction = result.instruction || this._instruction;
      this._validation = result.validation || null;
      this._status = "Settings saved.";
    } catch (err) {
      this._status = `Failed to save settings: ${err}`;
    }
    this._render();
  }

  async _checkAddon() {
    try {
      const result = await this._hass.callApi("GET", "home_assistant_agent/health");
      if (result.status === "success") {
        this._status = "Add-on is reachable.";
      } else {
        this._status = `Add-on check failed: ${result.error || "unknown error"}`;
      }
    } catch (err) {
      this._status = `Add-on check failed: ${err}`;
    }
    this._render();
  }

  async _runSuggest() {
    const input = this.shadowRoot.getElementById("llm-key");
    const llmKey = input.value || "";
    try {
      const result = await this._hass.callApi("POST", "home_assistant_agent/suggest", {
        llm_key: llmKey || undefined,
        use_llm: true,
        entities: this._entities,
      });
      this._suggestions = result;
      this._status = "Suggestions received.";
    } catch (err) {
      this._status = `Suggest failed: ${err}`;
    }
    this._render();
  }
}

customElements.define("home-assistant-agent-panel", HAAgentPanel);
