# Knowledge graph (graphify)

This repo ships a **committed knowledge graph** of the codebase under `graphify-out/`,
built by [graphify](https://github.com/Graphify-Labs/graphify). It captures the god
nodes (most-connected abstractions like `Orchestrator` and `FeedbackRecordModel`),
community structure, and cross-file relationships, and it is the first thing your AI
assistant is told to consult — see the `graphify` rules in
[`CLAUDE.md`](https://github.com/rodekruis/qualitative-feedback-analysis/blob/main/CLAUDE.md).

Committing the graph means everyone on the team — human or agent — starts from the same
map on a fresh clone, without paying to rebuild it locally.

## Install

graphify is a standalone CLI, not a project dependency (it never gets added to
`pyproject.toml`). Install it once on your machine:

```bash
uv tool install graphifyy        # recommended
# or: pipx install graphifyy
# or: pip install graphifyy
```

To pin to the upstream repository instead of PyPI — e.g. to track an unreleased fix:

```bash
uv tool install git+https://github.com/Graphify-Labs/graphify
```

Then register the graphify skill with your assistant so `/graphify` and the query
commands are available:

```bash
graphify install             # register globally
# or: graphify install --project   # register for this repo only
```

graphify needs **no API key** for code: the graph is extracted structurally from the AST,
which is deterministic and free. Only doc/image nodes use an LLM, and that falls back to
your assistant when no `GEMINI_API_KEY` is set.

## Team workflow

The graph is a shared artifact. The intended loop:

1. **One person builds and commits it.** Run `/graphify .` (or `graphify .`) and commit
   the resulting `graphify-out/`. This has already been done for this repo.
2. **Everyone pulls.** On the next `git pull`, your assistant reads the graph immediately —
   no rebuild, no cost.
3. **Install the git hooks once per clone** so the graph stays current automatically:

   ```bash
   graphify hook install
   ```

   This wires up a **post-commit** hook that re-extracts changed files (AST only, so no
   API cost) and a **git merge driver** so `graph.json` is never left with conflict
   markers — two developers committing in parallel get their graphs union-merged
   automatically. Check or remove the hooks with `graphify hook status` /
   `graphify hook uninstall`.
4. **Refresh docs/paper nodes when they change.** The post-commit hook only re-runs the
   free AST pass. When Markdown docs, ADRs, or images change materially, refresh their
   semantic nodes explicitly:

   ```bash
   graphify . --update
   ```

## What is committed vs. ignored

`graphify-out/` is committed, but a few per-machine and regenerable files are excluded in
[`.gitignore`](https://github.com/rodekruis/qualitative-feedback-analysis/blob/main/.gitignore):

| Path | Committed? | Why |
|------|-----------|-----|
| `graph.json` | ✅ | The queryable graph — what the assistant reads. `manifest.json` keys are relative, so it re-anchors on any clone. |
| `GRAPH_REPORT.md`, `manifest.json`, `graph.html`, `GRAPH_TREE.html` | ✅ | Audit report, update manifest, and the standalone HTML views (open in a browser, no server). |
| `cost.json` | ❌ | Cumulative token tally — local to whoever built the graph. |
| `.graphify_python`, `.graphify_root` | ❌ | Absolute interpreter and scan-root paths — only valid on the build machine. |
| `cache/` | ❌ | Regenerable AST/semantic cache, pinned to the graphify tool version. Optional: uncomment the line in `.gitignore` to commit it for a faster first `graphify . --update`. |

## Using the graph

Prefer the graph over raw grep when orienting in unfamiliar code — it returns a scoped
subgraph that is usually far smaller than a wide text search:

```bash
graphify query "how does an analyze request flow through the orchestrator?"
graphify path "Orchestrator" "SqlAlchemyUsageRepository"   # shortest relationship path
graphify explain "TenantApiKey"                            # focused explanation of one node
```

Read [`graphify-out/GRAPH_REPORT.md`](https://github.com/rodekruis/qualitative-feedback-analysis/blob/main/graphify-out/GRAPH_REPORT.md)
directly only for a broad architecture review; for targeted questions the query commands
are cheaper and sharper.
