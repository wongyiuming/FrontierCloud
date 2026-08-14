from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "Office-Automation"
    REDIS_URL: str = Field(..., validation_alias="REDIS_URL")
    WALL_ADMIN_TOKEN: str = Field(..., validation_alias="WALL_ADMIN_TOKEN")
    WALL_TTL: int = Field(240, validation_alias="WALL_TTL")

    MYSQL_URL: str = Field(
        "mysql+asyncmy://media_admin:change_me@mysql:3306/office_automation",
        validation_alias="MYSQL_URL",
    )

    ADMIN_TOKEN_TTL: int = Field(900, validation_alias="ADMIN_TOKEN_TTL")
    ADMIN_TOKEN_INITIAL_TTL: int = Field(86400, validation_alias="ADMIN_TOKEN_INITIAL_TTL")
    ADMIN_SESSION_TTL: int = Field(900, validation_alias="ADMIN_SESSION_TTL")
    ADMIN_MAX_FAILED_ATTEMPTS_PER_IP: int = Field(10, validation_alias="ADMIN_MAX_FAILED_ATTEMPTS_PER_IP")
    ADMIN_FAILED_WINDOW: int = Field(300, validation_alias="ADMIN_FAILED_WINDOW")
    ADMIN_MAX_UPLOAD_FILE_SIZE: int = Field(800 * 1024 * 1024, validation_alias="ADMIN_MAX_UPLOAD_FILE_SIZE")
    ADMIN_MAX_UPLOAD_TASK_FILES: int = Field(5000, validation_alias="ADMIN_MAX_UPLOAD_TASK_FILES")
    ADMIN_MAX_BATCH_FILES: int = Field(200, validation_alias="ADMIN_MAX_BATCH_FILES")
    ADMIN_MAX_DOWNLOAD_ITEMS: int = Field(100, validation_alias="ADMIN_MAX_DOWNLOAD_ITEMS")
    ADMIN_COOKIE_SECURE: bool = Field(True, validation_alias="ADMIN_COOKIE_SECURE")
    ADMIN_COOKIE_SAMESITE: str = Field("strict", validation_alias="ADMIN_COOKIE_SAMESITE")
    ADMIN_COOKIE_NAME: str = Field("__Host-admin_session", validation_alias="ADMIN_COOKIE_NAME")
    ADMIN_CSRF_COOKIE_NAME: str = Field("__Host-admin_csrf", validation_alias="ADMIN_CSRF_COOKIE_NAME")
    ADMIN_MAX_FILENAME_LENGTH: int = Field(240, validation_alias="ADMIN_MAX_FILENAME_LENGTH")
    MEDIA_MAX_DEPTH: int = Field(32, validation_alias="MEDIA_MAX_DEPTH")
    MEDIA_CATALOG_CACHE_TTL: int = Field(300, validation_alias="MEDIA_CATALOG_CACHE_TTL")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


settings = Settings()
