from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    PROJECT_NAME: str = "Office-Automation"
    ENVIRONMENT: str = Field("production", validation_alias="ENVIRONMENT")
    REDIS_URL: str = Field(..., validation_alias="REDIS_URL")
    WALL_ADMIN_TOKEN: str = Field(..., validation_alias="WALL_ADMIN_TOKEN")
    WALL_TTL: int = Field(240, validation_alias="WALL_TTL")

    MYSQL_URL: str = Field(..., validation_alias="MYSQL_URL")
    MYSQL_PASSWORD: str = Field(..., validation_alias="MYSQL_PASSWORD")
    MYSQL_ROOT_PASSWORD: str = Field(..., validation_alias="MYSQL_ROOT_PASSWORD")

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

    WATERMARK_MAX_FILES: int = Field(20, ge=1, validation_alias="WATERMARK_MAX_FILES")
    WATERMARK_MAX_UPLOAD_FILE_SIZE: int = Field(64 * 1024 * 1024, ge=1, validation_alias="WATERMARK_MAX_UPLOAD_FILE_SIZE")
    WATERMARK_MAX_UPLOAD_TOTAL_SIZE: int = Field(64 * 1024 * 1024, ge=1, validation_alias="WATERMARK_MAX_UPLOAD_TOTAL_SIZE")
    WATERMARK_MAX_ARCHIVE_FILES: int = Field(200, ge=1, validation_alias="WATERMARK_MAX_ARCHIVE_FILES")
    WATERMARK_MAX_ARCHIVE_FILE_SIZE: int = Field(64 * 1024 * 1024, ge=1, validation_alias="WATERMARK_MAX_ARCHIVE_FILE_SIZE")
    WATERMARK_MAX_ARCHIVE_TOTAL_SIZE: int = Field(128 * 1024 * 1024, ge=1, validation_alias="WATERMARK_MAX_ARCHIVE_TOTAL_SIZE")
    WATERMARK_MAX_IMAGE_PIXELS: int = Field(8_000_000, ge=1, validation_alias="WATERMARK_MAX_IMAGE_PIXELS")
    WATERMARK_MAX_PDF_PAGES: int = Field(200, ge=1, validation_alias="WATERMARK_MAX_PDF_PAGES")
    WATERMARK_MAX_TEXT_LENGTH: int = Field(200, ge=1, validation_alias="WATERMARK_MAX_TEXT_LENGTH")
    WATERMARK_MAX_CONCURRENT_JOBS: int = Field(1, ge=1, validation_alias="WATERMARK_MAX_CONCURRENT_JOBS")
    WATERMARK_MAX_WORKERS: int = Field(2, ge=1, validation_alias="WATERMARK_MAX_WORKERS")

    TRUSTED_PROXY_NETWORKS: str = Field("172.16.0.0/12", validation_alias="TRUSTED_PROXY_NETWORKS")
    SECURITY_EXEMPT_NETWORKS: str = Field("127.0.0.0/8,::1/128", validation_alias="SECURITY_EXEMPT_NETWORKS")
    SECURITY_INVALID_API_LIMIT: int = Field(5, validation_alias="SECURITY_INVALID_API_LIMIT")
    SECURITY_INVALID_API_WINDOW: int = Field(3600, validation_alias="SECURITY_INVALID_API_WINDOW")
    SECURITY_AUTO_BAN_TTL: int = Field(86400, validation_alias="SECURITY_AUTO_BAN_TTL")
    SECURITY_RECENT_BAN_HOURS: int = Field(24, validation_alias="SECURITY_RECENT_BAN_HOURS")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_security_settings(self):
        environment = self.ENVIRONMENT.strip().lower()
        if environment not in {"development", "test", "production"}:
            raise ValueError("ENVIRONMENT must be development, test, or production")
        self.ENVIRONMENT = environment

        if environment != "production":
            return self

        weak_values = {
            "change_me",
            "change_root_password",
            "password",
            "huawei@123",
        }

        def is_weak(value: str, minimum_length: int) -> bool:
            normalized = value.strip().lower()
            return (
                len(value) < minimum_length
                or normalized in weak_values
                or normalized.startswith(("replace_with", "change_me", "changeme"))
            )

        errors = []
        if is_weak(self.WALL_ADMIN_TOKEN, 32):
            errors.append("WALL_ADMIN_TOKEN must be a unique random secret of at least 32 characters")
        if is_weak(self.MYSQL_PASSWORD, 16):
            errors.append("MYSQL_PASSWORD must be a unique secret of at least 16 characters")
        if is_weak(self.MYSQL_ROOT_PASSWORD, 16):
            errors.append("MYSQL_ROOT_PASSWORD must be a unique secret of at least 16 characters")

        try:
            url_password = make_url(self.MYSQL_URL).password or ""
        except ArgumentError:
            errors.append("MYSQL_URL is invalid")
        else:
            if url_password != self.MYSQL_PASSWORD:
                errors.append("MYSQL_URL password must match MYSQL_PASSWORD")

        if not self.ADMIN_COOKIE_SECURE:
            errors.append("ADMIN_COOKIE_SECURE must be true in production")
        if self.ADMIN_COOKIE_SAMESITE.strip().lower() not in {"strict", "lax"}:
            errors.append("ADMIN_COOKIE_SAMESITE must be strict or lax in production")
        if not self.ADMIN_COOKIE_NAME.startswith("__Host-"):
            errors.append("ADMIN_COOKIE_NAME must use the __Host- prefix in production")
        if not self.ADMIN_CSRF_COOKIE_NAME.startswith("__Host-"):
            errors.append("ADMIN_CSRF_COOKIE_NAME must use the __Host- prefix in production")

        if errors:
            raise ValueError("; ".join(errors))
        return self


settings = Settings()
