from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    cors_origins: str = "http://localhost:3000,http://localhost:5173"
    download_dir: Path = Path("downloads")
    max_search_results: int = 25
    stream_url_ttl: int = 3600
    mp3_bitrate: int = 192
    # Optional: contents of a Netscape-format cookies.txt from a YouTube login.
    # Helps when YouTube blocks server IPs ("Sign in to confirm you're not a bot").
    yt_cookies: str = ""
    # Optional: proxy for all YouTube traffic, e.g. http://user:pass@host:port or socks5://...
    # Use a residential proxy — YouTube blocks datacenter IPs (Render, AWS, ...).
    yt_proxy: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
