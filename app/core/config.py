from pathlib import Path
from urllib.parse import quote

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRET_DIR = Path("/run/frontiercloud-secrets")
MYSQL_PASSWORD_FILE = SECRET_DIR / "mysql_password"
MYSQL_ROOT_PASSWORD_FILE = SECRET_DIR / "mysql_root_password"
ADMIN_KEY_FILE = SECRET_DIR / "admin_key"


def _read_secret(path: Path, fallback: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return fallback
    return value or fallback


class Settings(BaseSettings):
    TLS_ENABLED: bool = Field(False, validation_alias="TLS_ENABLED")
    REDIS_URL: str = Field("redis://redis:6379/0", validation_alias="REDIS_URL")
    MYSQL_URL: str | None = Field(None, validation_alias="MYSQL_URL")
    MYSQL_DATABASE: str = Field("office_automation", validation_alias="MYSQL_DATABASE")
    MYSQL_USER: str = Field("media_admin", validation_alias="MYSQL_USER")
    SERVER_NAME: str = Field("localhost", validation_alias="SERVER_NAME")
    ADMIN_SESSION_TTL: int = Field(900, ge=1, validation_alias="ADMIN_SESSION_TTL")
    ADMIN_MAX_FAILED_ATTEMPTS_PER_IP: int = Field(10, validation_alias="ADMIN_MAX_FAILED_ATTEMPTS_PER_IP")
    ADMIN_FAILED_WINDOW: int = Field(300, validation_alias="ADMIN_FAILED_WINDOW")
    ADMIN_MAX_UPLOAD_FILE_SIZE: int = Field(800 * 1024 * 1024, validation_alias="ADMIN_MAX_UPLOAD_FILE_SIZE")
    ADMIN_UPLOAD_INACTIVITY_TIMEOUT: int = Field(300, ge=1, validation_alias="ADMIN_UPLOAD_INACTIVITY_TIMEOUT")
    ADMIN_MAX_UPLOAD_TASK_FILES: int = Field(5000, validation_alias="ADMIN_MAX_UPLOAD_TASK_FILES")
    ADMIN_MAX_BATCH_FILES: int = Field(200, validation_alias="ADMIN_MAX_BATCH_FILES")
    ADMIN_MAX_DOWNLOAD_ITEMS: int = Field(100, validation_alias="ADMIN_MAX_DOWNLOAD_ITEMS")
    ADMIN_COOKIE_SAMESITE: str = Field("strict", validation_alias="ADMIN_COOKIE_SAMESITE")
    ADMIN_MAX_FILENAME_LENGTH: int = Field(240, validation_alias="ADMIN_MAX_FILENAME_LENGTH")
    MEDIA_MAX_DEPTH: int = Field(32, validation_alias="MEDIA_MAX_DEPTH")
    MEDIA_CATALOG_CACHE_TTL: int = Field(300, validation_alias="MEDIA_CATALOG_CACHE_TTL")
    WEBRTC_STUN_PORT: int = Field(3478, ge=1, le=65535, validation_alias="WEBRTC_STUN_PORT")
    WEBRTC_REPORT_COOLDOWN: int = Field(30, ge=10, le=3600, validation_alias="WEBRTC_REPORT_COOLDOWN")
    METRICS_TOKEN: str = Field("", validation_alias="METRICS_TOKEN")
    LOG_LEVEL: str = Field("INFO", validation_alias="LOG_LEVEL")
    LOG_FORMAT: str = Field("json", validation_alias="LOG_FORMAT")
    INSTANCE_NAME: str = Field("frontiercloud", validation_alias="INSTANCE_NAME")
    TRUSTED_PROXY_NETWORKS: str = Field("172.16.0.0/12", validation_alias="TRUSTED_PROXY_NETWORKS")
    SECURITY_EXEMPT_NETWORKS: str = Field("127.0.0.0/8,::1/128", validation_alias="SECURITY_EXEMPT_NETWORKS")
    SECURITY_INVALID_API_LIMIT: int = Field(5, validation_alias="SECURITY_INVALID_API_LIMIT")
    SECURITY_INVALID_API_WINDOW: int = Field(3600, validation_alias="SECURITY_INVALID_API_WINDOW")
    SECURITY_RECENT_BAN_HOURS: int = Field(24, validation_alias="SECURITY_RECENT_BAN_HOURS")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=True, extra="ignore")

    @model_validator(mode="after")
    def validate_settings(self):
        errors: list[str] = []
        if self.TLS_ENABLED and self.SERVER_NAME.strip().lower() in {"", "localhost"}:
            errors.append("SERVER_NAME must be set to the public hostname when TLS_ENABLED=true")
        if not self.MYSQL_URL:
            user = quote(self.MYSQL_USER, safe="")
            password = quote(_read_secret(MYSQL_PASSWORD_FILE, "uninitialized"), safe="")
            database = quote(self.MYSQL_DATABASE, safe="")
            self.MYSQL_URL = f"mysql+asyncmy://{user}:{password}@mysql:3306/{database}"
        if self.ADMIN_COOKIE_SAMESITE.strip().lower() not in {"strict", "lax"}:
            errors.append("ADMIN_COOKIE_SAMESITE must be strict or lax")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        return normalized

    @field_validator("LOG_FORMAT")
    @classmethod
    def validate_log_format(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"json", "text"}:
            raise ValueError("LOG_FORMAT must be json or text")
        return normalized

    @property
    def ADMIN_COOKIE_SECURE(self) -> bool:
        return self.TLS_ENABLED

    @property
    def ADMIN_COOKIE_NAME(self) -> str:
        return "__Host-admin_session" if self.TLS_ENABLED else "admin_session"

    @property
    def ADMIN_CSRF_COOKIE_NAME(self) -> str:
        return "__Host-admin-csrf" if self.TLS_ENABLED else "admin_csrf"

    def webrtc_stun_urls(self) -> list[str]:
        return [f"stun:{self.SERVER_NAME}:{self.WEBRTC_STUN_PORT}"]


settings = Settings()
