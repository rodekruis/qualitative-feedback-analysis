import logging
from enum import Enum
from typing import Any

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from qfa.domain.models import TenantApiKey


class LogSettings(BaseSettings):
    """Define settings for the logger."""

    loglevel: int = logging.DEBUG  # loglevel for "our" packages
    loglevel_3rdparty: int = logging.WARNING  # loglevel for 3rdparty packages
    our_packages: list[str] = [
        # list of "our" packages
        "__main__",
        "qfa",
    ]
    basicConfig: dict[str, Any] = {
        # "basicConfig" of the logging module.
        # Do not include the level parameter here since it's being controlled
        # by the loglevel... parameters above.
        "format": "%(asctime)s:%(levelname)s:%(name)s:%(message)s",
    }

    @field_validator("loglevel", "loglevel_3rdparty", mode="before")
    @classmethod
    def string_to_loglevel(cls, v: str) -> int:
        """Convert a string to a loglevel."""
        try:
            return int(v)
        except (TypeError, ValueError):
            v = v.lower()
            if v == "debug":
                return logging.DEBUG
            elif v == "info":
                return logging.INFO
            elif v == "warning":
                return logging.WARNING
            elif v == "error":
                return logging.ERROR
            elif v == "critical":
                return logging.CRITICAL
            else:
                raise ValueError(f"invalid loglevel {v}")


class LLMProvider(str, Enum):
    """Supported LLM provider backends."""

    OPENAI = "openai"
    AZURE_OPENAI = "azure_openai"


class LLMSettings(BaseSettings):
    """Configuration for the LLM provider connection."""

    model_config = SettingsConfigDict(env_prefix="LLM_")

    provider: LLMProvider = LLMProvider.OPENAI
    model: str = "gpt-4.1-mini"
    api_key: SecretStr  # required, no default
    azure_endpoint: str = ""
    api_version: str = ""
    timeout_seconds: float = 115.0
    max_retries: int = 3
    max_total_tokens: int = 100_000

    @model_validator(mode="after")
    def _azure_fields_required(self) -> "LLMSettings":
        if self.provider == LLMProvider.AZURE_OPENAI:
            if not self.azure_endpoint:
                raise ValueError(
                    "LLM_AZURE_ENDPOINT is required when LLM_PROVIDER is 'azure_openai'"
                )
            if not self.api_version:
                raise ValueError(
                    "LLM_API_VERSION is required when LLM_PROVIDER is 'azure_openai'"
                )
        return self


class OrchestratorSettings(BaseSettings):
    """Configuration for the orchestrator service."""

    model_config = SettingsConfigDict(env_prefix="ORCHESTRATOR_")

    metadata_fields_to_include: list[str] = Field(default_factory=list)
    retry_base_seconds: float = 1.0
    retry_multiplier: float = 2.0
    retry_jitter_factor: float = 0.5
    retry_cap_seconds: float = 10.0
    chars_per_token: int = 4


class AuthSettings(BaseSettings):
    """Configuration for API-key based authentication."""

    model_config = SettingsConfigDict(env_prefix="AUTH_")

    api_keys: list[TenantApiKey]  # required, no default


class DatabaseSettings(BaseSettings):
    """Configuration for the PostgreSQL database connection.

    Attributes
    ----------
    url : str
        Database connection URL (asyncpg dialect).
    track_usage : bool
        Feature flag to enable/disable usage tracking.
    """

    model_config = SettingsConfigDict(env_prefix="DB_")

    url: str = ""
    track_usage: bool = False


class NetworkSettings(BaseSettings):
    """Configuration for network settings."""

    model_config = SettingsConfigDict(env_prefix="NETWORK_")
    host: str = "0.0.0.0"  # noqa: S104 (hardcoded-bind-all-interfaces)
    port: int = 8000


class AppSettings(BaseSettings):
    """Root configuration composing all sub-settings groups."""

    llm: LLMSettings = Field(default_factory=LLMSettings)
    orchestrator: OrchestratorSettings = Field(default_factory=OrchestratorSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    network: NetworkSettings = Field(default_factory=NetworkSettings)
    debug: bool = False
    """Whether to enable debug mode.
    
    This will, e.g., enable code reloading for the uvicorn server.
    """
