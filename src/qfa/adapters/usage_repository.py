"""SQLAlchemy-backed usage repository.

Reads aggregated LLM-call statistics from the ``llm_calls`` table
declared in :mod:`qfa.adapters.db`. The repository exposes two views:

- **Per-invocation** — one entry per distinct ``call_id``, so a single
  REST API call that fans out to N LLM calls counts once.
- **Per-LLM-call** (``llm_call_stats``) — one entry per raw LLM call
  attempt.

Both views are produced from the same row set in one ``SELECT`` per
view, using a Postgres CTE (for per-invocation) plus ``GROUPING SETS``
to roll up to ``(tenant, operation)`` / ``(tenant)`` / ``()`` in a
single round-trip.

Internally, both views share one SELECT builder, one row parser, and
one pivot — only the SQL *source* differs (raw ``llm_calls`` vs a CTE
pre-aggregated by ``call_id``). The duality is expressed once as a
``view: Literal["llm", "inv"]`` parameter rather than carried through
as parallel code paths.
"""

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Literal, overload

import sqlalchemy as sa
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from qfa.adapters.db import llm_calls
from qfa.domain.errors import UsageRepositoryUnavailableError
from qfa.domain.models import (
    DistributionStats,
    LLMCallRecord,
    Operation,
    OperationStats,
    OperationUsageStats,
    TenantStats,
    TokenStats,
    UsageMetrics,
    UsageStats,
)
from qfa.domain.ports import UsageRepositoryPort

# Flat ``(tenant_id, operation) -> metrics`` dict carrying the GROUPING
# SETS rollup rows from the SQL layer into the pivot. ``None`` in either
# tuple position marks a rollup cell (e.g. ``(tenant_id, None)`` is the
# per-tenant subtotal; ``(None, None)`` is the grand total).
_StatsKey = tuple[str | None, str | None]
_StatsByKey = dict[_StatsKey, UsageMetrics]


@asynccontextmanager
async def _translate_db_errors() -> AsyncIterator[None]:
    """Translate SQLAlchemy connectivity errors into a domain-level error.

    Wraps the read paths so the API layer can map a single domain
    exception (``UsageRepositoryUnavailableError``) to ``503 {"code":
    "usage_backend_unavailable"}`` without importing SQLAlchemy. Write
    paths (``record_call``) are intentionally not wrapped: the
    ``TrackingLLMAdapter`` already swallows recording failures so the
    LLM response still flows back to the user.
    """
    try:
        yield
    except (OperationalError, InterfaceError) as exc:
        raise UsageRepositoryUnavailableError(str(exc)) from exc


class SqlAlchemyUsageRepository(UsageRepositoryPort):
    """Usage repository backed by SQLAlchemy and PostgreSQL.

    Parameters
    ----------
    session_factory : Callable[..., AsyncSession]
        Factory for creating async database sessions.
    """

    def __init__(self, session_factory: Callable[..., AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_call(self, record: LLMCallRecord) -> None:
        """Insert a single LLM call attempt record."""
        async with self._session_factory() as session:
            await session.execute(
                llm_calls.insert().values(
                    tenant_id=record.tenant_id,
                    operation=str(record.operation),
                    call_id=record.call_id,
                    timestamp=record.timestamp,
                    call_duration_ms=record.call_duration_ms,
                    model=record.model,
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    cost_usd=record.cost_usd,
                    status=str(record.status),
                    error_class=record.error_class,
                )
            )
            await session.commit()

    async def get_usage_stats(
        self,
        tenant_id: str,
        from_: datetime | None = None,
        to: datetime | None = None,
    ) -> UsageStats:
        """Per-invocation and per-LLM-call stats for one tenant.

        Single-tenant SELECT pair grouped by operation only. Returns a
        zero ``UsageStats`` when no rows match the window.
        """
        base_pred = self._base_predicates(tenant_id=tenant_id, from_=from_, to=to)
        inv, llm = await self._fetch_views(
            base_pred, group_by_tenant=False, group_by_operation=True
        )
        return self._build_block(
            top="tenant", top_value=tenant_id, inv_by_key=inv, llm_by_key=llm
        )

    async def get_all_usage_stats(
        self,
        from_: datetime | None = None,
        to: datetime | None = None,
    ) -> list[UsageStats]:
        """Per-tenant + grand-total stats with per-operation breakdown.

        Cross-tenant SELECT pair grouping by ``(tenant_id, operation)``,
        ``(tenant_id)``, ``(operation)``, and ``()`` (the full 2-axis
        cube) so each view returns every required level in one round-trip.
        Tenants with zero per-invocation calls are filtered; the
        grand-total entry (``tenant_id=None``) is always emitted last.
        """
        base_pred = self._base_predicates(from_=from_, to=to)
        inv, llm = await self._fetch_views(
            base_pred, group_by_tenant=True, group_by_operation=True
        )
        tenants = sorted({t for (t, op) in inv if t is not None and op is None})

        out: list[UsageStats] = []
        for t in tenants:
            block = self._build_block(
                top="tenant", top_value=t, inv_by_key=inv, llm_by_key=llm
            )
            if block.total_calls > 0:
                out.append(block)

        # Grand total — always emitted, even when empty.
        out.append(
            self._build_block(
                top="tenant", top_value=None, inv_by_key=inv, llm_by_key=llm
            )
        )
        return out

    async def get_all_usage_by_operation(
        self,
        from_: datetime | None = None,
        to: datetime | None = None,
    ) -> list[OperationUsageStats]:
        """Per-operation + grand-total stats with per-tenant breakdown.

        Inverse hierarchy of :meth:`get_all_usage_stats`: same query
        pair (full 2-axis cube), but the pivot puts ``operation`` at
        the top level with ``tenants`` nested. The grand-total entry
        (``operation=None``) is always emitted last.
        """
        base_pred = self._base_predicates(from_=from_, to=to)
        inv, llm = await self._fetch_views(
            base_pred, group_by_tenant=True, group_by_operation=True
        )
        operations = sorted({op for (t, op) in inv if op is not None and t is None})

        out: list[OperationUsageStats] = []
        for op in operations:
            block = self._build_block(
                top="operation", top_value=op, inv_by_key=inv, llm_by_key=llm
            )
            if block.total_calls > 0:
                out.append(block)

        # Grand total — always emitted, even when empty.
        out.append(
            self._build_block(
                top="operation", top_value=None, inv_by_key=inv, llm_by_key=llm
            )
        )
        return out

    # ------------------------------------------------------------------
    # Predicates
    # ------------------------------------------------------------------

    @staticmethod
    def _base_predicates(
        *,
        tenant_id: str | None = None,
        from_: datetime | None = None,
        to: datetime | None = None,
    ) -> list:
        """Build the half-open ``[from, to)`` window + optional tenant predicate."""
        pred: list = []
        if tenant_id is not None:
            pred.append(llm_calls.c.tenant_id == tenant_id)
        if from_ is not None:
            pred.append(llm_calls.c.timestamp >= from_)
        if to is not None:
            pred.append(llm_calls.c.timestamp < to)
        return pred

    # ------------------------------------------------------------------
    # Fetch + index
    # ------------------------------------------------------------------

    async def _fetch_views(
        self,
        base_pred: list,
        *,
        group_by_tenant: bool,
        group_by_operation: bool,
    ) -> tuple[_StatsByKey, _StatsByKey]:
        """Run both view SELECTs and return ``(inv_by_key, llm_by_key)``.

        Issues two queries — the per-invocation view (CTE on ``call_id``)
        and the per-LLM-call view — against the same predicate set and
        grouping, and indexes their rows by ``(tenant_id, operation)``
        with ``None`` marking GROUPING SETS rollup cells.
        """
        async with _translate_db_errors(), self._session_factory() as session:
            inv_rows = (
                await session.execute(
                    self._select_view(
                        "inv",
                        base_pred,
                        group_by_tenant=group_by_tenant,
                        group_by_operation=group_by_operation,
                    )
                )
            ).all()
            llm_rows = (
                await session.execute(
                    self._select_view(
                        "llm",
                        base_pred,
                        group_by_tenant=group_by_tenant,
                        group_by_operation=group_by_operation,
                    )
                )
            ).all()
        return self._index_rows(inv_rows), self._index_rows(llm_rows)

    @staticmethod
    def _index_rows(rows: Sequence[sa.Row]) -> _StatsByKey:
        """Index aggregate rows by ``(tenant_id, operation)``.

        Missing columns (e.g. ``tenant_id`` on a single-tenant SELECT
        that doesn't group by tenant) and NULL rollup values both map
        to ``None`` — the two are indistinguishable from the consumer's
        perspective and that's what the pivot expects.
        """
        out: _StatsByKey = {}
        for r in rows:
            m = r._mapping
            t = m.get("tenant_id")
            op = m.get("operation")
            out[(t, op)] = SqlAlchemyUsageRepository._row_to_usage_metrics(r)
        return out

    # ------------------------------------------------------------------
    # SELECT builder (single function for both views)
    # ------------------------------------------------------------------

    @staticmethod
    def _select_view(
        view: Literal["llm", "inv"],
        base_pred: list,
        *,
        group_by_tenant: bool,
        group_by_operation: bool,
    ) -> sa.Select:
        """Build the aggregation SELECT for one of the two views.

        ``view='llm'`` aggregates the raw ``llm_calls`` table — one row
        per LLM call attempt. ``view='inv'`` aggregates a per-invocation
        CTE (one row per distinct ``call_id``, with token/duration/cost
        summed across the LLM calls of the invocation and a ``bool_and``
        flag marking the all-failed case).

        Both views emit identical, canonically-labelled aggregate columns
        (``total_calls``, ``failed_calls``, ``total_cost_usd``, plus
        ``dur_*``/``inp_*``/``out_*`` distributions) so a single row
        parser consumes either.

        Counts and ``total_cost_usd`` include every row in scope —
        including failures that incurred a real cost. Distributions
        filter to the "ok" subset (single-row ``status='ok'`` for the
        ``llm`` view; "not all calls in this invocation failed" for the
        ``inv`` view) so failures cannot skew latency or token quantiles.
        """
        if view == "inv":
            per_invocation = (
                sa.select(
                    llm_calls.c.tenant_id.label("tenant_id"),
                    llm_calls.c.operation.label("operation"),
                    llm_calls.c.call_id.label("call_id"),
                    sa.func.sum(llm_calls.c.call_duration_ms).label("dur"),
                    sa.func.sum(llm_calls.c.input_tokens).label("inp"),
                    sa.func.sum(llm_calls.c.output_tokens).label("out"),
                    sa.func.sum(llm_calls.c.cost_usd).label("cost"),
                    sa.func.bool_and(llm_calls.c.status == "error").label("all_failed"),
                )
                .where(*base_pred)
                .group_by(
                    llm_calls.c.tenant_id,
                    llm_calls.c.operation,
                    llm_calls.c.call_id,
                )
                .cte("per_invocation")
            )
            tenant_col: sa.ColumnElement = per_invocation.c.tenant_id
            op_col: sa.ColumnElement = per_invocation.c.operation
            ok_filter: sa.ColumnElement = per_invocation.c.all_failed.is_(False)
            err_filter: sa.ColumnElement = per_invocation.c.all_failed.is_(True)
            dur_col: sa.ColumnElement = per_invocation.c.dur
            inp_col: sa.ColumnElement = per_invocation.c.inp
            out_col: sa.ColumnElement = per_invocation.c.out
            cost_col: sa.ColumnElement = per_invocation.c.cost
            outer_from: sa.FromClause = per_invocation
            outer_where: list = []  # predicates already applied inside the CTE
        else:
            tenant_col = llm_calls.c.tenant_id
            op_col = llm_calls.c.operation
            ok_filter = llm_calls.c.status == "ok"
            err_filter = llm_calls.c.status == "error"
            dur_col = llm_calls.c.call_duration_ms
            inp_col = llm_calls.c.input_tokens
            out_col = llm_calls.c.output_tokens
            cost_col = llm_calls.c.cost_usd
            outer_from = llm_calls
            outer_where = base_pred

        cols: list[sa.ColumnElement] = []
        if group_by_tenant:
            cols.append(tenant_col.label("tenant_id"))
        if group_by_operation:
            cols.append(op_col.label("operation"))
        cols.extend(
            [
                sa.func.count().label("total_calls"),
                sa.func.count().filter(err_filter).label("failed_calls"),
                sa.func.coalesce(sa.func.sum(cost_col), 0).label("total_cost_usd"),
                *SqlAlchemyUsageRepository._build_stats_columns(
                    dur_col, "dur", where=ok_filter
                ),
                *SqlAlchemyUsageRepository._build_stats_columns(
                    inp_col, "inp", where=ok_filter
                ),
                *SqlAlchemyUsageRepository._build_stats_columns(
                    out_col, "out", where=ok_filter
                ),
            ]
        )

        stmt = sa.select(*cols).select_from(outer_from)
        if outer_where:
            stmt = stmt.where(*outer_where)
        grouping_sets = SqlAlchemyUsageRepository._grouping_sets_clause(
            group_by_tenant=group_by_tenant,
            group_by_operation=group_by_operation,
        )
        if grouping_sets is not None:
            stmt = stmt.group_by(grouping_sets)
        return stmt

    @staticmethod
    def _grouping_sets_clause(
        *,
        group_by_tenant: bool,
        group_by_operation: bool,
    ) -> sa.TextClause | None:
        """Build the ``GROUPING SETS (...)`` clause for the requested grouping.

        When both axes are requested this emits the full 2-axis cube
        — equivalent to ``CUBE(tenant_id, operation)`` — because
        ``/v1/usage/all/by-operation`` needs the ``(operation)`` rollup
        cell and ``/v1/usage/all/by-tenant`` needs the ``(tenant)`` one.

        Returns ``None`` for the degenerate no-grouping case (currently
        unused; kept as a defensive shortcut).
        """
        sets: list[str] = []
        if group_by_tenant and group_by_operation:
            sets.extend(["(tenant_id, operation)", "(tenant_id)", "(operation)", "()"])
        elif group_by_operation:
            sets.extend(["(operation)", "()"])
        elif group_by_tenant:
            sets.extend(["(tenant_id)", "()"])
        else:
            return None
        return sa.text(f"GROUPING SETS ({', '.join(sets)})")

    @staticmethod
    def _build_stats_columns(
        col: sa.ColumnElement,
        prefix: str,
        *,
        where: sa.ColumnElement | None = None,
    ) -> list[sa.Label]:
        """Build avg/min/max/sum/count/p5/p95 labelled aggregations for *col*.

        When ``where`` is supplied, ``FILTER (WHERE ...)`` is applied to
        every aggregate so the same SELECT can mix all-row counts with
        ok-only distributions.
        """

        def _f(agg: sa.ColumnElement) -> sa.ColumnElement:
            return agg.filter(where) if where is not None else agg

        return [
            _f(sa.func.avg(col)).label(f"{prefix}_avg"),
            _f(sa.func.min(col)).label(f"{prefix}_min"),
            _f(sa.func.max(col)).label(f"{prefix}_max"),
            _f(sa.func.sum(col)).label(f"{prefix}_sum"),
            _f(sa.func.count()).label(f"{prefix}_count"),
            _f(sa.func.percentile_cont(0.05).within_group(col)).label(f"{prefix}_p5"),
            _f(sa.func.percentile_cont(0.95).within_group(col)).label(f"{prefix}_p95"),
        ]

    # ------------------------------------------------------------------
    # Row → domain parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_distribution(row: sa.Row, prefix: str) -> DistributionStats:
        """Parse ``DistributionStats`` from a row's ok-only aggregates.

        When no ok rows exist, ``avg`` is NULL — return zeros.
        """
        m = row._mapping
        avg = m[f"{prefix}_avg"]
        if avg is None:
            return DistributionStats(avg=0, min=0, max=0, p5=0, p95=0)
        return DistributionStats(
            avg=float(avg),
            min=float(m[f"{prefix}_min"]),
            max=float(m[f"{prefix}_max"]),
            p5=float(m[f"{prefix}_p5"]),
            p95=float(m[f"{prefix}_p95"]),
        )

    @staticmethod
    def _parse_token_stats(row: sa.Row, prefix: str) -> TokenStats:
        """Parse ``TokenStats`` from a row's ok-only aggregates.

        When no ok rows exist, ``avg`` is NULL — return zeros (``total=0``).
        """
        m = row._mapping
        avg = m[f"{prefix}_avg"]
        if avg is None:
            return TokenStats(avg=0, min=0, max=0, p5=0, p95=0, total=0)
        return TokenStats(
            avg=float(avg),
            min=float(m[f"{prefix}_min"]),
            max=float(m[f"{prefix}_max"]),
            total=int(m[f"{prefix}_sum"] or 0),
            p5=float(m[f"{prefix}_p5"]),
            p95=float(m[f"{prefix}_p95"]),
        )

    @staticmethod
    def _row_to_usage_metrics(row: sa.Row) -> UsageMetrics:
        """Build a ``UsageMetrics`` from a row's canonically-labelled aggregates.

        Both views emit the same column names (``total_calls``,
        ``failed_calls``, ``total_cost_usd``, ``dur_*``/``inp_*``/``out_*``),
        so the same parser consumes either. NULL aggregates (the all-failed
        or empty-window case) become zeros.
        """
        m = row._mapping
        return UsageMetrics(
            total_calls=int(m["total_calls"] or 0),
            failed_calls=int(m["failed_calls"] or 0),
            total_cost_usd=Decimal(str(m["total_cost_usd"] or 0)),
            call_duration=SqlAlchemyUsageRepository._parse_distribution(row, "dur"),
            input_tokens=SqlAlchemyUsageRepository._parse_token_stats(row, "inp"),
            output_tokens=SqlAlchemyUsageRepository._parse_token_stats(row, "out"),
        )

    @staticmethod
    def _zero_usage_metrics() -> UsageMetrics:
        """Zero ``UsageMetrics`` used as the fallback for missing roll-up rows."""
        return UsageMetrics(
            total_calls=0,
            failed_calls=0,
            total_cost_usd=Decimal("0"),
            call_duration=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0),
            input_tokens=TokenStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
            output_tokens=TokenStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        )

    # ------------------------------------------------------------------
    # Pivot (single function for both axes)
    # ------------------------------------------------------------------

    @overload
    @staticmethod
    def _build_block(
        *,
        top: Literal["tenant"],
        top_value: str | None,
        inv_by_key: _StatsByKey,
        llm_by_key: _StatsByKey,
    ) -> UsageStats: ...

    @overload
    @staticmethod
    def _build_block(
        *,
        top: Literal["operation"],
        top_value: str | None,
        inv_by_key: _StatsByKey,
        llm_by_key: _StatsByKey,
    ) -> OperationUsageStats: ...

    @staticmethod
    def _build_block(
        *,
        top: Literal["tenant", "operation"],
        top_value: str | None,
        inv_by_key: _StatsByKey,
        llm_by_key: _StatsByKey,
    ) -> UsageStats | OperationUsageStats:
        """Pivot the flat (tenant, op) key dicts into one top-level block.

        Parameters
        ----------
        top
            Which axis is the top-level discriminator. The other axis
            supplies the nested breakdown rows.
        top_value
            The value of the top axis for this block — a tenant id, an
            operation, or ``None`` for the grand total.
        inv_by_key, llm_by_key
            Per-invocation and per-LLM-call metrics keyed by
            ``(tenant_id, operation)``; ``None`` in either position marks
            a GROUPING SETS rollup cell.

        Returns
        -------
        ``UsageStats`` when ``top='tenant'``, ``OperationUsageStats`` when
        ``top='operation'``. Top-level metrics come from the
        ``(top_value, None)`` / ``(None, top_value)`` rollup cell.
        Breakdown rows come from the (tenant, op) cells where the top
        axis matches and the other axis is bound (non-None); zero-call
        cells are omitted and the result is sorted by
        ``total_cost_usd`` desc, with ties broken by the child
        discriminator asc.
        """
        zero = SqlAlchemyUsageRepository._zero_usage_metrics
        rollup_key: _StatsKey = (
            (top_value, None) if top == "tenant" else (None, top_value)
        )
        top_inv = inv_by_key.get(rollup_key) or zero()
        top_llm = llm_by_key.get(rollup_key) or zero()

        if top == "tenant":
            operations: list[OperationStats] = []
            for (t, op), inv in inv_by_key.items():
                if t != top_value or op is None or inv.total_calls == 0:
                    continue
                operations.append(
                    OperationStats(
                        operation=Operation(op),
                        **dict(inv),
                        llm_call_stats=llm_by_key.get((t, op)) or zero(),
                    )
                )
            operations.sort(key=lambda o: (-o.total_cost_usd, o.operation.value))
            return UsageStats(
                tenant_id=top_value,
                **dict(top_inv),
                llm_call_stats=top_llm,
                operations=tuple(operations),
            )

        tenants: list[TenantStats] = []
        for (t, op), inv in inv_by_key.items():
            if op != top_value or t is None or inv.total_calls == 0:
                continue
            tenants.append(
                TenantStats(
                    tenant_id=t,
                    **dict(inv),
                    llm_call_stats=llm_by_key.get((t, op)) or zero(),
                )
            )
        tenants.sort(key=lambda b: (-b.total_cost_usd, b.tenant_id))
        return OperationUsageStats(
            operation=Operation(top_value) if top_value is not None else None,
            **dict(top_inv),
            llm_call_stats=top_llm,
            tenants=tuple(tenants),
        )
