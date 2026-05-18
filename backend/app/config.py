from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── API Keys ──────────────────────────────────────────────────────────────
    gemini_api_key: str = ""          # https://aistudio.google.com/apikey
    tavily_api_key: str = ""          # https://app.tavily.com

    # ── LLM Models ───────────────────────────────────────────────────────────
    primary_model: str = "gemini-3.1-flash-lite"
    fallback_model: str = "gemini-2.5-flash-lite"

    # ── App ───────────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    cors_origins: list[str] = ["http://localhost:5173"]

    # ── Ollama (local LLM) ────────────────────────────────────────────────────
    # ollama_base_url: str = "http://localhost:11434"
    # primary_model: str = "qwen3:8b"
    # fallback_model: str = "llama3.1:8b"


settings = Settings()
