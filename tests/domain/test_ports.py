"""Structural-conformance tests for domain ports.

These confirm the port protocols are importable and that a hand-written
fake can satisfy them structurally (the contract test fakes rely on),
so adapters and services can depend on the port rather than a concrete
class.
"""

from qfa.domain.ports import EmbeddingPort


def test_embedding_port_is_runtime_checkable_protocol() -> None:
    """A structural fake with a matching ``embed`` satisfies ``EmbeddingPort``.

    Why: test fakes (``FakeEmbeddingPort``) intentionally conform
    structurally without inheriting the port, so the orchestrator can be
    unit-tested with no real model. This locks that contract in.
    """

    class _Fake:
        def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
            return tuple((float(len(t)),) for t in texts)

    fake = _Fake()
    vectors = fake.embed(("a", "bb"))
    assert vectors == ((1.0,), (2.0,))
    # Structural typing: a value that has ``embed`` is usable where an
    # EmbeddingPort is expected. ``isinstance`` requires runtime_checkable.
    assert isinstance(fake, EmbeddingPort)
