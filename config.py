from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    supabase_url: str
    supabase_service_key: str
    anthropic_api_key: str
    openai_api_key: str
    cairn_api_key: str
    exa_api_key: str = ""
    similarity_threshold: float = 0.92
    max_dedup_attempts: int = 3
    coverage_cache_minutes: int = 5

    class Config:
        env_file = ".env"


settings = Settings()
