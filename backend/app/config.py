from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ollama_base_url: str = "http://localhost:11434"
    primary_model: str = "qwen3:8b"
    fallback_model: str = "llama3.1:8b"
    log_level: str = "INFO"
    cors_origins: list[str] = ["http://localhost:5173"]


settings = Settings()
