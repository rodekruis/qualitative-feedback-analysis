"""Driven adapters for :class:`~qfa.domain.ports.EvaluationPort`.

Two implementations:

* :class:`LangfuseEvaluationAdapter` — sends scores to a self-hosted
  Langfuse instance via the ``langfuse`` Python client.
* :class:`NoOpEvaluationAdapter` — discards every score. The composition
  root selects this one when ``LANGFUSE_PUBLIC_KEY``/``LANGFUSE_SECRET_KEY``
  are unset, mirroring how :func:`qfa.api.composition.build_langfuse_tracer`
  degrades to an unattached tracer in the same case. Every call site holds
  one or the other, never ``None`` — a Null Object, not an optional port,
  so no call site branches on whether Langfuse is configured.
"""

import logging

from langfuse import Langfuse
from opentelemetry.sdk.trace import TracerProvider

from qfa.domain.ports import EvaluationPort
from qfa.settings import LangfuseSettings

logger = logging.getLogger(__name__)


class LangfuseEvaluationAdapter(EvaluationPort):
    """Sends judge-quality scores to a self-hosted Langfuse instance.

    ``Langfuse.create_score`` does no synchronous I/O — it appends to an
    in-process queue that a background thread flushes — and catches every
    exception itself rather than raising, so a Langfuse outage never
    reaches the caller. That satisfies #354's failure requirement ("a
    Langfuse outage must never fail an analysis") by construction, without
    this adapter needing its own try/except or timeout. The queue's own
    ``atexit`` hook handles shutdown, the same pattern already relied on
    for ``PresidioAnonymizer``'s thread pool in
    :func:`qfa.api.composition.build_services` — no explicit teardown is
    wired into the FastAPI lifespan.

    Passes its own, independent ``TracerProvider`` — never the
    process-global one :func:`qfa.telemetry.configure_telemetry` may
    install for Application Insights. Left unset, the ``langfuse`` client
    attaches its span processor to whatever ``TracerProvider`` is already
    registered globally; with Application Insights configured, that is the
    Azure Monitor provider, so every App Insights span (DB statements,
    outbound HTTP calls) would also be exported to Langfuse. Mirrors
    :func:`qfa.api.composition.build_langfuse_tracer`'s own isolated
    provider, built for the same reason.
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

    def record_score(self, *, trace_id: str, name: str, value: float) -> None:
        """Record one named score against ``trace_id`` in Langfuse."""
        self._client.create_score(
            trace_id=trace_id,
            name=name,
            value=value,
            data_type="NUMERIC",
        )


class NoOpEvaluationAdapter(EvaluationPort):
    """Discards every score. The default when Langfuse is not configured."""

    def record_score(self, *, trace_id: str, name: str, value: float) -> None:
        """Discard the score; ``trace_id``/``name``/``value`` are unused."""
        return None
