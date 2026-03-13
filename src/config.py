from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str
    database_url: str
    app_port: int = 8000
    health_user_id: str = "owner"  # user_id for Apple Health data
    llm_provider: str = "openai"   # active provider: "openai" | "anthropic" | "gemini"
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    groq_api_key: str = ""
    # User whitelist (comma-separated). Empty = open access (not recommended).
    # Example: ALLOWED_USER_IDS=195374487,987654321
    allowed_user_ids: str = ""
    # Internal key for /pipeline/* endpoints (n8n → app). Empty = no check (insecure).
    internal_api_key: str = ""
    # Agent name — used in system prompts and logs
    app_name: str = "Health Agent"
    # Timeout for acquiring a connection from the DB pool (seconds)
    db_acquire_timeout: float = 5.0
    # Models by task (can be overridden via .env)
    agent_model: str = "anthropic/claude-haiku-4-5-20251001"       # analytics, /ask, /report, /mind
    cheap_model: str = "openai/gpt-4o-mini"                        # parsing food, measurements, dates
    vision_food_model: str = "openai/gpt-4o-mini"                  # food photos
    vision_medical_model: str = "anthropic/claude-haiku-4-5-20251001"  # medical documents
    embedding_model: str = "text-embedding-3-small"                  # embeddings (OpenAI)

    model_config = {"env_file": "/app/.env", "extra": "ignore"}


settings = Settings()
