# ADR-014: EmbeddingPort and self-hosted BGE-M3 ONNX embedding model

## Status

Accepted

## Context

`mode: "hierarchical"` (#124) analyses corpora larger than the single-call
token cap by clustering feedback records into thematically coherent chunks
before a map-reduce over the LLM. Clustering needs a vector per record, so
we need a text-embedding model. The model is the one genuinely external,
swappable dependency in the new path; everything downstream of the vectors
(clustering, the coding-trend table) is deterministic in-process computation.

Constraints from the domain: feedback is multilingual, so the model must be
multilingual (a monolingual model clusters by language, not theme); the
service runs CPU-only; and we must not weaken our data-handling posture
(anonymisation runs before embedding, and the model must not be able to
execute arbitrary code or exfiltrate data).

## Decision

1. Introduce a single new **driven port**, `EmbeddingPort`
   (`qfa.domain.ports`), a synchronous `typing.Protocol`:
   `embed(texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]`.
   Synchronous because encoding is CPU-bound local computation, not I/O
   (contrast `LLMPort.complete`). Domain-pure (plain floats, no numpy in
   the signature).
2. Implement it with `BgeM3OnnxEmbedder` (`qfa.adapters.embedding`), running
   **BGE-M3, ONNX, int8**, in-process via `onnxruntime`, **dense-1024-d
   output only**. The adapter **explicitly inherits `EmbeddingPort`** per
   AGENTS.md.
3. **Artifact provenance:** a community pre-built ONNX-int8 build, **pinned
   by revision hash**, **mirrored** into our own artifact store, and loaded
   from that mirror — never fetched from HuggingFace at runtime in
   production. **Validated once** against official `BAAI/bge-m3` (cosine >=
   0.999) to catch a botched/altered conversion.
4. **Security flags asserted at construction:** `trust_remote_code=False`;
   **no** custom-operator libraries registered; keep onnxruntime patched. A
   standard-op ONNX graph cannot execute arbitrary code or perform I/O
   (unlike a pickle `.bin` checkpoint), so the residual surface is parser
   CVEs (patching) and conversion correctness (the one-time validation).
5. **Concurrency:** a single batched `session.run()` with
   `intra_op_num_threads` left at the core count. No thread/process pool.
6. **Clustering and the coding-trend table get no port** — they are
   deterministic `services` logic with nothing external to swap, unit-tested
   with hand-built vectors and zero mocks.

## Options considered

### A. EmbeddingPort + self-hosted ONNX adapter (chosen)

- **Pro:** keeps the heavyweight model out of the application ring; the port
  preserves the option to externalise the model later without touching
  `services`. CPU-only, no torch at runtime, <800 MB footprint.
- **Pro:** self-hosting avoids sending feedback to a third-party embedding
  API — consistent with the anonymise-before-LLM posture.
- **Con:** we own the mirroring + one-time validation as an ops concern.

### B. Hosted embedding API (e.g. provider endpoint) — rejected

- **Con:** sends (even anonymised) feedback off-box for a task we can do
  in-process; adds an I/O dependency and per-call cost for no quality win at
  our scale.

### C. Self-convert official MIT weights via `optimum` up front — deferred

- The artifact source lives entirely behind the port, so self-conversion is
  a **build/ops** change, not an architecture change (same code). It is
  build-expensive (re-introduces the ~2 GB torch toolchain, ~2.2 GB
  download, ~4-6 GB peak RAM) for no runtime benefit, so it is documented as
  a later, compliance-driven option only.

## Consequences

- `qfa.domain.ports` gains exactly one new driven port; the
  driving/driven split is preserved (ADR-011).
- `qfa.api.app` is the only place that constructs the adapter (allowlisted
  in the `import-linter` layers contract, like the other composition-root
  imports).
- Deployments that do not use `mode=hierarchical` set no `EMBEDDING_*`
  variables; the orchestrator gets `embedder=None` and hierarchical requests
  degrade to a 502 `analysis_unavailable` rather than requiring a model on
  disk.
- A future externalised or self-converted model is a drop-in: a new adapter
  behind the same port, no `services` change.

## Participants

Marius
