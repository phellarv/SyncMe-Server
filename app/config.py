from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # Deprecated: kept only so a leftover value can be detected and reported at startup.
    SYNCME_API_KEY: str | None = None
    SYNCME_KEYS_FILE: Path | None = None
    DATA_DIR: Path = Path("./data")
    STORAGE_DIR: Path | None = None
    FILES_DIR: Path | None = None
    ARCHIVE_DIR: Path | None = None
    DB_PATH: Path | None = None
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    MAX_UPLOAD_BYTES: int = 256 * 1024 * 1024
    TRUST_PROXY_HEADERS: bool = False
    BACKUP_DIR: Path | None = None
    BACKUP_KEEP: int = 0   # backups kept by `cli backup`; 0 keeps all

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,   # `NAME=` in .env means unset, not "" (an empty path would be the cwd)
    )


settings = Settings()

