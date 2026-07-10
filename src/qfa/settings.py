import logging
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from qfa.domain.clustering_models import TrendPeriod
from qfa.domain.models import TenantApiKey

#: Worst-case retry budget of a single ``LLMPort.complete`` call, expressed as a
#: multiple of the per-attempt ``timeout``. The LLM adapter retries transient
#: failures (timeout, rate-limit) up to ``LLM_RETRY_BUDGET_MULTIPLIER * timeout``
#: of wall-clock; the orchestrator divides the deadline-derived budget by the
#: same factor when sizing a per-attempt timeout, so even the worst-case retry
#: sequence of the last call in a phase still finishes before the request
#: deadline. The adapter and orchestrator MUST read this one constant so the two
#: stay in lock-step — they live in different layers and cannot share code.
LLM_RETRY_BUDGET_MULTIPLIER: float = 3.0

DEFAULT_EMBEDDING_BATCH_SIZE = 100
"""Default records-per-onnxruntime-batch for the embedder.

Single source of truth shared by ``EmbeddingSettings.batch_size`` and the
``qfa.adapters.embedding`` constructor/factory defaults, so the configurable
default and the library default cannot silently drift apart.
"""


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


class LLMSettings(BaseSettings):
    """Configuration for the LLM provider connection.

    The provider is inferred from the model string prefix by LiteLLM
    (e.g. ``"azure/gpt-4"`` for Azure OpenAI, ``"azure_ai/mistral-large"``
    for Azure AI serverless endpoints).
    """

    model_config = SettingsConfigDict(env_prefix="LLM_")

    model: str = "azure_ai/mistral-medium-2505"
    api_key: SecretStr = Field(default=...)  # required, no default
    api_base: str = ""
    api_version: str = ""
    timeout_seconds: float = 115.0
    max_total_tokens: int = 100_000
    chars_per_token: int = 4


class EmbeddingSettings(BaseSettings):
    """Configuration for the self-hosted embedding model.

    The artifact is mirrored locally and pinned by hash; production never
    fetches it from HuggingFace at runtime.
    """

    model_config = SettingsConfigDict(env_prefix="EMBEDDING_")

    model_path: str = ""
    tokenizer_path: str = ""
    revision_hash: str = ""
    model_kind: Literal["bge-m3", "e5"] = Field(
        default="e5",
        description=(
            "Embedding model *family*, which selects the adapter's output"
            " handling: ``e5`` (default) mean-pools the token-level"
            " ``last_hidden_state`` over the attention mask and prepends the"
            " ``query: `` prefix every E5 input requires; ``bge-m3`` takes the"
            " model's already-pooled ``dense_vecs`` head as-is. The dimension"
            " and token cap are *per-artifact* and set separately"
            " (``dense_dim`` / ``max_tokens``), so both e5-base (768-d) and"
            " e5-small (384-d) share ``kind=e5``. Default is e5-base: smaller"
            " and faster than BGE-M3 for a modest cross-lingual quality trade;"
            " set ``kind=bge-m3`` + ``dense_dim=1024`` to use the stronger"
            " model."
        ),
    )
    dense_dim: int = Field(
        default=768,
        ge=1,
        description=(
            "Expected output dimensionality of the dense vector, validated"
            " per batch so a mismatched artifact/config fails loud rather"
            " than silently producing wrong-width vectors. multilingual-e5-base"
            " (the default) is 768; multilingual-e5-small is 384; BGE-M3 is"
            " 1024."
        ),
    )
    max_tokens: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Tokenizer truncation cap. ``None`` uses the model family's"
            " natural context: 8192 for ``bge-m3``, 512 for ``e5`` (its"
            " XLM-R/MiniLM backbone's positional limit). Set an explicit"
            " lower value to bound the per-record (and, since padding is to"
            " the batch's longest row, per-batch) cost from long outliers."
        ),
    )
    intra_op_num_threads: int | None = None
    batch_size: int = Field(
        default=DEFAULT_EMBEDDING_BATCH_SIZE,
        ge=1,
        description=(
            "Records embedded per onnxruntime batch. The corpus is encoded in"
            " sequential batches of this size to bound peak memory on large"
            " inputs (padding is per-batch, so smaller batches also waste less)."
        ),
    )


class OrchestratorSettings(BaseSettings):
    """Cross-cutting configuration shared by every orchestrator use case.

    Per-endpoint tuning lives in its own settings class (e.g.
    :class:`AnalyzeSettings`) so the eventual split into
    one-use-case-per-module (per ADR-011) doesn't require renaming
    env-vars in production. Only knobs that genuinely apply to *every*
    use case — retry policy, token-budget estimation, metadata
    allow-list — stay here.
    """

    model_config = SettingsConfigDict(env_prefix="ORCHESTRATOR_")

    metadata_fields_to_include: list[str] = Field(default_factory=list)
    retry_base_seconds: float = 1.0
    retry_multiplier: float = 2.0
    retry_jitter_factor: float = 0.5
    retry_cap_seconds: float = 10.0
    chars_per_token: int = 4


class AnalyzeSettings(BaseSettings):
    """Configuration specific to the ``POST /v1/analyze`` endpoint.

    Covers both ``mode=single_pass`` and ``mode=hierarchical``: the
    coding-trend table is built for both, and the clustering knobs are
    only consulted on the hierarchical path. Naming the group after the
    endpoint (not the mode) lets a future single_pass-only knob land
    here without another rename.
    """

    model_config = SettingsConfigDict(env_prefix="ANALYZE_")

    min_cluster_size: int = Field(
        default=5,
        ge=2,
        description=(
            "HDBSCAN min_cluster_size for the map-step chunking"
            " (mode=hierarchical only)."
        ),
    )
    clustering_metric: str = Field(
        default="euclidean",
        description=(
            "HDBSCAN distance metric over dense embedding vectors"
            " (mode=hierarchical only)."
        ),
    )
    max_concurrent_chunks: int = Field(
        default=64,
        ge=1,
        description=(
            "Maximum map-step chunks analysed concurrently (mode=hierarchical)."
            " Each chunk is one analysis LLM call plus one leaf-judge call, so"
            " this bounds the fan-out and keeps a large corpus from bursting"
            " past the provider's request/token rate limit. Set to 1 for a"
            " fully sequential map."
        ),
    )
    target_chunk_tokens: int = Field(
        default=2_000,
        ge=1,
        description=(
            "Target size (in estimated tokens) for a single map chunk"
            " (mode=hierarchical). This is the chunking *granularity* knob,"
            " deliberately decoupled from the LLM hard cap LLM_MAX_TOTAL_TOKENS:"
            " HDBSCAN produces uneven clusters, so without a target a single"
            " dominant theme becomes one fat map call whose latency (it runs"
            " concurrently with the others) sets the wall-clock tail. A cluster"
            " larger than this is split into roughly equal, date-ordered"
            " sub-chunks. The effective split budget is"
            " min(target_chunk_tokens, LLM_MAX_TOTAL_TOKENS), so a chunk can"
            " never exceed what one call can hold regardless of this value."
            " Lower it for more, smaller, more-parallel calls; raise it for"
            " fewer, larger calls."
        ),
    )
    coding_trend_code_fields: list[str] = Field(
        default_factory=lambda: ["coding_level_1", "coding_level_2", "coding_level_3"],
        description="Metadata keys holding coding labels (comma-separated strings).",
    )
    default_coding_trend_period: TrendPeriod = Field(
        default="week",
        description=(
            "Server-side default granularity for the coding-trend table."
            " Callers can override per-request via the analyze request"
            " body's ``period`` field. ``week`` is usually right; ``month``"
            " suits multi-year corpora; ``day`` short-window deep-dives."
        ),
    )


class AuthSettings(BaseSettings):
    """Configuration for API-key based authentication."""

    model_config = SettingsConfigDict(env_prefix="AUTH_")

    api_keys: list[TenantApiKey] = Field(default=...)  # required, no default


class DatabaseSettings(BaseSettings):
    """Configuration for the PostgreSQL database connection.

    Attributes
    ----------
    url : str
        Database connection URL (asyncpg dialect).
    """

    model_config = SettingsConfigDict(env_prefix="DB_")

    url: str = ""
    host: str = ""
    port: int = 5432
    name: str = ""
    user: str = ""
    password: SecretStr | None = None
    auth_mode: Literal["password", "entra"] = "password"
    aad_scope: str = "https://ossrdbms-aad.database.windows.net/.default"

    @model_validator(mode="after")
    def _require_url_or_parts(self) -> "DatabaseSettings":
        if self.url:
            return self

        if not self.host:
            raise ValueError("DB_HOST must be set when DB_URL is not provided")
        if not self.user:
            raise ValueError("DB_USER must be set when DB_URL is not provided")
        if not self.name:
            raise ValueError("DB_NAME must be set when DB_URL is not provided")
        if self.port <= 0:
            raise ValueError("DB_PORT must be greater than 0")
        if self.auth_mode == "password" and self.password is None:
            raise ValueError(
                "DB_PASSWORD must be set when DB_AUTH_MODE=password "
                "and DB_URL is not provided"
            )
        return self


class NetworkSettings(BaseSettings):
    """Configuration for network settings."""

    model_config = SettingsConfigDict(env_prefix="NETWORK_")
    host: str = "0.0.0.0"  # noqa: S104 (hardcoded-bind-all-interfaces)
    port: int = 8000


class TelemetrySettings(BaseSettings):
    """Azure Monitor / Application Insights telemetry configuration.

    Kept as its own group with no required fields so it can be constructed
    standalone at import time (``qfa.main`` reads it before building the full
    application graph) without tripping the required env-vars of the other
    settings groups.
    """

    # No env_prefix: the Azure App Service injects the fixed variable name
    # APPLICATIONINSIGHTS_CONNECTION_STRING, which maps to the field below.
    model_config = SettingsConfigDict()

    applicationinsights_connection_string: SecretStr | None = None
    """Azure Application Insights connection string.

    Read from ``APPLICATIONINSIGHTS_CONNECTION_STRING`` (set on the App Service
    by ``infra/app_service.tf``). When present, ``qfa.main`` initialises the
    Azure Monitor OpenTelemetry SDK with it; unset in local dev, which leaves
    telemetry export disabled. Held as ``SecretStr`` — it embeds an ingestion
    key — so it is masked in logs and ``model_dump`` output.
    """


class AppSettings(BaseSettings):
    """Root configuration composing all sub-settings groups."""

    llm: LLMSettings = Field(default_factory=LLMSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    orchestrator: OrchestratorSettings = Field(default_factory=OrchestratorSettings)
    analyze: AnalyzeSettings = Field(default_factory=AnalyzeSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    network: NetworkSettings = Field(default_factory=NetworkSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    debug: bool = False
    """Whether to enable debug mode.

    This will, e.g., enable code reloading for the uvicorn server.
    """
