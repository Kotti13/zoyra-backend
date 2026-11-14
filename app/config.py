from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    cors_origins: str = "http://localhost:3000,http://localhost:5173"
    download_dir: Path = Path("downloads")
    max_search_results: int = 25
    stream_url_ttl: int = 3600
    mp3_bitrate: int = 192

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
