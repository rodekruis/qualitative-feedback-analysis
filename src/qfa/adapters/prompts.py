"""Driven adapters for :class:`~qfa.domain.ports.PromptPort`.

Two implementations, mirroring :mod:`qfa.adapters.evaluation`:

* :class:`LangfusePromptAdapter` — mirrors hardcoded prompt text
  (:mod:`qfa.services.prompt_registry`) to a self-hosted Langfuse instance as
  versioned Text prompts, for the version numbers ``GET /v1/health`` and the
  Langfuse trace attributes expose. It never fetches prompt text back.
* :class:`NoOpPromptAdapter` — the Null Object the composition root
  (:func:`qfa.api.composition.build_prompt_versions`) selects when
  ``LANGFUSE_PUBLIC_KEY``/``LANGFUSE_SECRET_KEY`` are unset, mirroring how
  :class:`~qfa.adapters.evaluation.NoOpEvaluationAdapter` degrades.
"""

import logging
from collections.abc import Mapping

from langfuse import Langfuse
from langfuse.api import NotFoundError
from opentelemetry.sdk.trace import TracerProvider

from qfa.domain.ports import PromptPort
from qfa.settings import LangfuseSettings

logger = logging.getLogger(__name__)

_PRODUCTION_LABEL = "production"


class LangfusePromptAdapter(PromptPort):
    """Mirrors hardcoded system prompts to Langfuse as versioned Text prompts.

    ``Langfuse.create_prompt`` creates a new version on every call — neither
    the server nor the client deduplicates by content (verified against the
    installed ``langfuse`` SDK; see ADR-023). Calling it unconditionally at
    every app startup would create a fresh, identical version on every
    restart, so :meth:`sync` compares against the current ``"production"``
    version first and calls ``create_prompt`` only when the text differs.
    """

    def __init__(self, settings: LangfuseSettings) -> None:
        self._client = Langfuse(
            public_key=settings.public_key,
            secret_key=(
                settings.secret_key.get_secret_value()
                if settings.secret_key is not None
                else None
            ),
            host=settings.host,
            tracer_provider=TracerProvider(),
        )

    async def sync(self, prompts: Mapping[str, str]) -> dict[str, int]:
        """Push every ``name: text`` pair whose ``"production"`` version differs.

        One ``get_prompt`` round trip per name, plus one ``create_prompt``
        round trip for a name that is missing or out of date. A name whose
        lookup or create call raises an unexpected exception is logged by
        name only — never with the prompt text, which a provider error could
        otherwise echo back — and left out of the returned dict, so one bad
        name cannot block the rest.
        """
        versions: dict[str, int] = {}
        for name, text in prompts.items():
            try:
                versions[name] = self._sync_one(name, text)
            except Exception as exc:
                logger.warning(
                    "Langfuse prompt sync failed for %r: error_class=%s",
                    name,
                    type(exc).__name__,
                )
        return versions

    def _sync_one(self, name: str, text: str) -> int:
        """Sync one prompt name; raises on an unrecovered lookup/create failure."""
        try:
            existing = self._client.get_prompt(name, label=_PRODUCTION_LABEL)
        except NotFoundError:
            existing = None

        if existing is not None and existing.prompt == text:
            return existing.version

        created = self._client.create_prompt(
            name=name, prompt=text, labels=[_PRODUCTION_LABEL]
        )
        return created.version


class NoOpPromptAdapter(PromptPort):
    """Discards every prompt. The default when Langfuse is not configured."""

    async def sync(self, prompts: Mapping[str, str]) -> dict[str, int]:
        """Return ``{}`` for any input; never invents a placeholder version."""
        return {}
