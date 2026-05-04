from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "thaiund"
    POSTGRES_USER: str = "thaiuser"
    POSTGRES_PASSWORD: str

    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440

    FIRST_ADMIN_EMAIL: str
    FIRST_ADMIN_PASSWORD: str

    SEC_API_KEY: str = ""               # Fund Daily Info API key
    SEC_FACTSHEET_KEY: str = ""         # Fund Factsheet API key (separate subscription)

    # fallback: use SEC_API_KEY when SEC_FACTSHEET_KEY not set
    @property
    def factsheet_key(self) -> str:
        return self.SEC_FACTSHEET_KEY or self.SEC_API_KEY

    # Secondary key for failover (optional)
    SEC_API_KEY_SECONDARY: str = ""

    OLLAMA_URL: str = "http://host.docker.internal:11434"
    OLLAMA_MODEL: str = "gemma4:26b"

    FINNOMENA_EMAIL: str = ""
    FINNOMENA_PASSWORD: str = ""

    @property
    def DATABASE_URL(self) -> str:
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def SYNC_DATABASE_URL(self) -> str:
        return (
            f"postgresql+psycopg2://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
