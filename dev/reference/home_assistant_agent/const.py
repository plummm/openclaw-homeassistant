"""Constants for the Home Assistant Agent integration."""

DOMAIN = "home_assistant_agent"

CONF_BASE_URL = "base_url"
CONF_LLM_KEY = "llm_key"
CONF_AUTH_KEY = "auth_key"
CONF_SET_DEFAULT_AGENT = "set_default_agent"
CONF_OPENAI_KEY = "openai_key"
CONF_ANTHROPIC_KEY = "anthropic_key"
CONF_GEMINI_KEY = "gemini_key"
CONF_MODEL_REASONING = "model_reasoning"
CONF_MODEL_FAST = "model_fast"
CONF_TTS_MODEL = "tts_model"
CONF_STT_MODEL = "stt_model"
CONF_INSTRUCTION = "instruction"

DEFAULT_BASE_URL = "http://core-ha_agent_core"

DEFAULT_INSTRUCTION = (
    "You are Home Assistant Agent, a helpful assistant embedded in Home Assistant. "
    "Your job is to help the user operate their smart home, answer questions, and "
    "suggest automations when relevant. Be concise, accurate, and action-oriented. "
    "If an action could be destructive or unsafe, ask for confirmation first."
)

PANEL_FRONTEND_URL = "home-assistant-agent"
PANEL_TITLE = "Home Assistant Agent"
PANEL_ICON = "mdi:robot"
PANEL_COMPONENT_NAME = "home-assistant-agent-panel"
PANEL_MODULE_URL = "/home_assistant_agent_panel/home-assistant-agent-panel.js"
