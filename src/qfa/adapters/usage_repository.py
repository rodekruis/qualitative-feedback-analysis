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
``view: Literal["llm_call", "invocation"]`` parameter rather than
carried through as parallel code paths.
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
        predicates = self._base_predicates(tenant_id=tenant_id, from_=from_, to=to)
        invocation_by_key, llm_call_by_key = await self._fetch_views(
            predicates, group_by_tenant=False, group_by_operation=True
        )
        return self._build_block(
            top_axis="tenant",
            top_value=tenant_id,
            invocation_by_key=invocation_by_key,
            llm_call_by_key=llm_call_by_key,
        )

    async def get_all_usage_by_tenant(
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
        predicates = self._base_predicates(from_=from_, to=to)
        invocation_by_key, llm_call_by_key = await self._fetch_views(
            predicates, group_by_tenant=True, group_by_operation=True
        )
        tenants = sorted(
            {
                tenant_id
                for (tenant_id, operation) in invocation_by_key
                if tenant_id is not None and operation is None
            }
        )

        results: list[UsageStats] = []
        for tenant_id in tenants:
            block = self._build_block(
                top_axis="tenant",
                top_value=tenant_id,
                invocation_by_key=invocation_by_key,
                llm_call_by_key=llm_call_by_key,
            )
            if block.total_calls > 0:
                results.append(block)

        # Grand total — always emitted, even when empty.
        results.append(
            self._build_block(
                top_axis="tenant",
                top_value=None,
                invocation_by_key=invocation_by_key,
                llm_call_by_key=llm_call_by_key,
            )
        )
        return results

    async def get_all_usage_by_operation(
        self,
        from_: datetime | None = None,
        to: datetime | None = None,
    ) -> list[OperationUsageStats]:
        """Per-operation + grand-total stats with per-tenant breakdown.

        Inverse hierarchy of :meth:`get_all_usage_by_tenant`: same query
        pair (full 2-axis cube), but the pivot puts ``operation`` at
        the top level with ``tenants`` nested. The grand-total entry
        (``operation=None``) is always emitted last.
        """
        predicates = self._base_predicates(from_=from_, to=to)
        invocation_by_key, llm_call_by_key = await self._fetch_views(
            predicates, group_by_tenant=True, group_by_operation=True
        )
        operations = sorted(
            {
                operation
                for (tenant_id, operation) in invocation_by_key
                if operation is not None and tenant_id is None
            }
        )

        results: list[OperationUsageStats] = []
        for operation in operations:
            block = self._build_block(
                top_axis="operation",
                top_value=operation,
                invocation_by_key=invocation_by_key,
                llm_call_by_key=llm_call_by_key,
            )
            if block.total_calls > 0:
                results.append(block)

        # Grand total — always emitted, even when empty.
        results.append(
            self._build_block(
                top_axis="operation",
                top_value=None,
                invocation_by_key=invocation_by_key,
                llm_call_by_key=llm_call_by_key,
            )
        )
        return results

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
        predicates: list = []
        if tenant_id is not None:
            predicates.append(llm_calls.c.tenant_id == tenant_id)
        if from_ is not None:
            predicates.append(llm_calls.c.timestamp >= from_)
        if to is not None:
            predicates.append(llm_calls.c.timestamp < to)
        return predicates

    # ------------------------------------------------------------------
    # Fetch + index
    # ------------------------------------------------------------------

    async def _fetch_views(
        self,
        predicates: list,
        *,
        group_by_tenant: bool,
        group_by_operation: bool,
    ) -> tuple[_StatsByKey, _StatsByKey]:
        """Run both view SELECTs and return ``(invocation_by_key, llm_call_by_key)``.

        Issues two queries — the per-invocation view (CTE on ``call_id``)
        and the per-LLM-call view — against the same predicate set and
        grouping, and indexes their rows by ``(tenant_id, operation)``
        with ``None`` marking GROUPING SETS rollup cells.
        """
        async with _translate_db_errors(), self._session_factory() as session:
            invocation_rows = (
                await session.execute(
                    self._select_view(
                        "invocation",
                        predicates,
                        group_by_tenant=group_by_tenant,
                        group_by_operation=group_by_operation,
                    )
                )
            ).all()
            llm_call_rows = (
                await session.execute(
                    self._select_view(
                        "llm_call",
                        predicates,
                        group_by_tenant=group_by_tenant,
                        group_by_operation=group_by_operation,
                    )
                )
            ).all()
        return (
            self._index_rows(invocation_rows),
            self._index_rows(llm_call_rows),
        )

    @staticmethod
    def _index_rows(rows: Sequence[sa.Row]) -> _StatsByKey:
        """Index aggregate rows by ``(tenant_id, operation)``.

        Missing columns (e.g. ``tenant_id`` on a single-tenant SELECT
        that doesn't group by tenant) and NULL rollup values both map
        to ``None`` — the two are indistinguishable from the consumer's
        perspective and that's what the pivot expects.
        """
        indexed: _StatsByKey = {}
        for row in rows:
            mapping = row._mapping
            tenant_id = mapping.get("tenant_id")
            operation = mapping.get("operation")
            indexed[(tenant_id, operation)] = (
                SqlAlchemyUsageRepository._row_to_usage_metrics(row)
            )
        return indexed

    # ------------------------------------------------------------------
    # SELECT builder (single function for both views)
    # ------------------------------------------------------------------

    @staticmethod
    def _select_view(
        view: Literal["llm_call", "invocation"],
        predicates: list,
        *,
        group_by_tenant: bool,
        group_by_operation: bool,
    ) -> sa.Select:
        """Build the aggregation SELECT for one of the two views.

        ``view='llm_call'`` aggregates the raw ``llm_calls`` table — one
        row per LLM call attempt. ``view='invocation'`` aggregates a
        per-invocation CTE (one row per distinct ``call_id``, with
        token/duration/cost summed across the LLM calls of the invocation
        and a ``bool_and`` flag marking the all-failed case).

        Both views emit identical, canonically-labelled aggregate columns
        (``total_calls``, ``failed_calls``, ``total_cost_usd``, plus
        ``duration_*``/``input_tokens_*``/``output_tokens_*`` distributions)
        so a single row parser consumes either.

        Counts and ``total_cost_usd`` include every row in scope —
        including failures that incurred a real cost. Distributions
        filter to the "ok" subset (single-row ``status='ok'`` for the
        ``llm_call`` view; "not all calls in this invocation failed" for
        the ``invocation`` view) so failures cannot skew latency or token
        quantiles.
        """
        if view == "invocation":
            per_invocation = (
                sa.select(
                    llm_calls.c.tenant_id.label("tenant_id"),
                    llm_calls.c.operation.label("operation"),
                    llm_calls.c.call_id.label("call_id"),
                    sa.func.sum(llm_calls.c.call_duration_ms).label("duration"),
                    sa.func.sum(llm_calls.c.input_tokens).label("input_tokens"),
                    sa.func.sum(llm_calls.c.output_tokens).label("output_tokens"),
                    sa.func.sum(llm_calls.c.cost_usd).label("cost"),
                    sa.func.bool_and(llm_calls.c.status == "error").label("all_failed"),
                )
                .where(*predicates)
                .group_by(
                    llm_calls.c.tenant_id,
                    llm_calls.c.operation,
                    llm_calls.c.call_id,
                )
                .cte("per_invocation")
            )
            tenant_col: sa.ColumnElement = per_invocation.c.tenant_id
            operation_col: sa.ColumnElement = per_invocation.c.operation
            ok_filter: sa.ColumnElement = per_invocation.c.all_failed.is_(False)
            err_filter: sa.ColumnElement = per_invocation.c.all_failed.is_(True)
            duration_col: sa.ColumnElement = per_invocation.c.duration
            input_tokens_col: sa.ColumnElement = per_invocation.c.input_tokens
            output_tokens_col: sa.ColumnElement = per_invocation.c.output_tokens
            cost_col: sa.ColumnElement = per_invocation.c.cost
            outer_from: sa.FromClause = per_invocation
            outer_where: list = []  # predicates already applied inside the CTE
        else:
            tenant_col = llm_calls.c.tenant_id
            operation_col = llm_calls.c.operation
            ok_filter = llm_calls.c.status == "ok"
            err_filter = llm_calls.c.status == "error"
            duration_col = llm_calls.c.call_duration_ms
            input_tokens_col = llm_calls.c.input_tokens
            output_tokens_col = llm_calls.c.output_tokens
            cost_col = llm_calls.c.cost_usd
            outer_from = llm_calls
            outer_where = predicates

        columns: list[sa.ColumnElement] = []
        if group_by_tenant:
            columns.append(tenant_col.label("tenant_id"))
        if group_by_operation:
            columns.append(operation_col.label("operation"))
        columns.extend(
            [
                sa.func.count().label("total_calls"),
                sa.func.count().filter(err_filter).label("failed_calls"),
                sa.func.coalesce(sa.func.sum(cost_col), 0).label("total_cost_usd"),
                *SqlAlchemyUsageRepository._build_stats_columns(
                    duration_col, "duration", where=ok_filter
                ),
                *SqlAlchemyUsageRepository._build_stats_columns(
                    input_tokens_col, "input_tokens", where=ok_filter
                ),
                *SqlAlchemyUsageRepository._build_stats_columns(
                    output_tokens_col, "output_tokens", where=ok_filter
                ),
            ]
        )

        statement = sa.select(*columns).select_from(outer_from)
        if outer_where:
            statement = statement.where(*outer_where)
        grouping_sets = SqlAlchemyUsageRepository._grouping_sets_clause(
            group_by_tenant=group_by_tenant,
            group_by_operation=group_by_operation,
        )
        if grouping_sets is not None:
            statement = statement.group_by(grouping_sets)
        return statement

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
        set_clauses: list[str] = []
        if group_by_tenant and group_by_operation:
            set_clauses.extend(
                [
                    "(tenant_id, operation)",
                    "(tenant_id)",
                    "(operation)",
                    "()",
                ]
            )
        elif group_by_operation:
            set_clauses.extend(["(operation)", "()"])
        elif group_by_tenant:
            set_clauses.extend(["(tenant_id)", "()"])
        else:
            return None
        return sa.text(f"GROUPING SETS ({', '.join(set_clauses)})")

    @staticmethod
    def _build_stats_columns(
        column: sa.ColumnElement,
        prefix: str,
        *,
        where: sa.ColumnElement | None = None,
    ) -> list[sa.Label]:
        """Build avg/min/max/sum/count/p5/p95 labelled aggregations for *column*.

        When ``where`` is supplied, ``FILTER (WHERE ...)`` is applied to
        every aggregate so the same SELECT can mix all-row counts with
        ok-only distributions.
        """

        def apply_where(aggregate: sa.ColumnElement) -> sa.ColumnElement:
            return aggregate.filter(where) if where is not None else aggregate

        return [
            apply_where(sa.func.avg(column)).label(f"{prefix}_avg"),
            apply_where(sa.func.min(column)).label(f"{prefix}_min"),
            apply_where(sa.func.max(column)).label(f"{prefix}_max"),
            apply_where(sa.func.sum(column)).label(f"{prefix}_sum"),
            apply_where(sa.func.count()).label(f"{prefix}_count"),
            apply_where(sa.func.percentile_cont(0.05).within_group(column)).label(
                f"{prefix}_p5"
            ),
            apply_where(sa.func.percentile_cont(0.95).within_group(column)).label(
                f"{prefix}_p95"
            ),
        ]

    # ------------------------------------------------------------------
    # Row → domain parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_distribution(row: sa.Row, prefix: str) -> DistributionStats:
        """Parse ``DistributionStats`` from a row's ok-only aggregates.

        When no ok rows exist, ``avg`` is NULL — return zeros. ``total``
        reads ``{prefix}_sum`` which is always emitted by
        :meth:`_build_stats_columns`.
        """
        mapping = row._mapping
        avg = mapping[f"{prefix}_avg"]
        if avg is None:
            return DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0)
        return DistributionStats(
            avg=float(avg),
            min=float(mapping[f"{prefix}_min"]),
            max=float(mapping[f"{prefix}_max"]),
            p5=float(mapping[f"{prefix}_p5"]),
            p95=float(mapping[f"{prefix}_p95"]),
            total=int(mapping[f"{prefix}_sum"] or 0),
        )

    @staticmethod
    def _row_to_usage_metrics(row: sa.Row) -> UsageMetrics:
        """Build a ``UsageMetrics`` from a row's canonically-labelled aggregates.

        Both views emit the same column names (``total_calls``,
        ``failed_calls``, ``total_cost_usd``, ``duration_*``/
        ``input_tokens_*``/``output_tokens_*``), so the same parser
        consumes either. NULL aggregates (the all-failed or empty-window
        case) become zeros.
        """
        mapping = row._mapping
        return UsageMetrics(
            total_calls=int(mapping["total_calls"] or 0),
            failed_calls=int(mapping["failed_calls"] or 0),
            total_cost_usd=Decimal(str(mapping["total_cost_usd"] or 0)),
            call_duration=SqlAlchemyUsageRepository._parse_distribution(
                row, "duration"
            ),
            input_tokens=SqlAlchemyUsageRepository._parse_distribution(
                row, "input_tokens"
            ),
            output_tokens=SqlAlchemyUsageRepository._parse_distribution(
                row, "output_tokens"
            ),
        )

    @staticmethod
    def _zero_usage_metrics() -> UsageMetrics:
        """Zero ``UsageMetrics`` used as the fallback for missing roll-up rows."""
        return UsageMetrics(
            total_calls=0,
            failed_calls=0,
            total_cost_usd=Decimal("0"),
            call_duration=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
            input_tokens=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
            output_tokens=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        )

    # ------------------------------------------------------------------
    # Pivot (single function for both axes)
    # ------------------------------------------------------------------

    @overload
    @staticmethod
    def _build_block(
        *,
        top_axis: Literal["tenant"],
        top_value: str | None,
        invocation_by_key: _StatsByKey,
        llm_call_by_key: _StatsByKey,
    ) -> UsageStats: ...

    @overload
    @staticmethod
    def _build_block(
        *,
        top_axis: Literal["operation"],
        top_value: str | None,
        invocation_by_key: _StatsByKey,
        llm_call_by_key: _StatsByKey,
    ) -> OperationUsageStats: ...

    @staticmethod
    def _build_block(
        *,
        top_axis: Literal["tenant", "operation"],
        top_value: str | None,
        invocation_by_key: _StatsByKey,
        llm_call_by_key: _StatsByKey,
    ) -> UsageStats | OperationUsageStats:
        """Pivot the flat (tenant, op) key dicts into one top-level block.

        Parameters
        ----------
        top_axis
            Which axis is the top-level discriminator. The other axis
            supplies the nested breakdown rows.
        top_value
            The value of the top axis for this block — a tenant id, an
            operation, or ``None`` for the grand total.
        invocation_by_key, llm_call_by_key
            Per-invocation and per-LLM-call metrics keyed by
            ``(tenant_id, operation)``; ``None`` in either position marks
            a GROUPING SETS rollup cell.

        Returns
        -------
        ``UsageStats`` when ``top_axis='tenant'``, ``OperationUsageStats``
        when ``top_axis='operation'``. Top-level metrics come from the
        ``(top_value, None)`` / ``(None, top_value)`` rollup cell.
        Breakdown rows come from the ``(tenant_id, operation)`` cells
        where the top axis matches and the other axis is bound
        (non-None); zero-call cells are omitted and the result is sorted by
        ``total_cost_usd`` desc, with ties broken by the child
        discriminator asc.
        """
        zero = SqlAlchemyUsageRepository._zero_usage_metrics
        rollup_key: _StatsKey = (
            (top_value, None) if top_axis == "tenant" else (None, top_value)
        )
        top_invocation = invocation_by_key.get(rollup_key) or zero()
        top_llm_call = llm_call_by_key.get(rollup_key) or zero()

        if top_axis == "tenant":
            operations: list[OperationStats] = []
            for (
                tenant_id,
                operation,
            ), invocation in invocation_by_key.items():
                if (
                    tenant_id != top_value
                    or operation is None
                    or invocation.total_calls == 0
                ):
                    continue
                operations.append(
                    OperationStats(
                        operation=Operation(operation),
                        **dict(invocation),
                        llm_call_stats=llm_call_by_key.get((tenant_id, operation))
                        or zero(),
                    )
                )
            operations.sort(key=lambda o: (-o.total_cost_usd, o.operation.value))
            return UsageStats(
                tenant_id=top_value,
                **dict(top_invocation),
                llm_call_stats=top_llm_call,
                operations=tuple(operations),
            )

        tenants: list[TenantStats] = []
        for (tenant_id, operation), invocation in invocation_by_key.items():
            if (
                operation != top_value
                or tenant_id is None
                or invocation.total_calls == 0
            ):
                continue
            tenants.append(
                TenantStats(
                    tenant_id=tenant_id,
                    **dict(invocation),
                    llm_call_stats=llm_call_by_key.get((tenant_id, operation))
                    or zero(),
                )
            )
        tenants.sort(key=lambda t: (-t.total_cost_usd, t.tenant_id))
        return OperationUsageStats(
            operation=Operation(top_value) if top_value is not None else None,
            **dict(top_invocation),
            llm_call_stats=top_llm_call,
            tenants=tuple(tenants),
        )
