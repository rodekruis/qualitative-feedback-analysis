"""HTTP-layer wrappers over the domain usage stats.

ADR-007 keeps API and domain models separate where the API needs to
hide internal fields or reshape the wire format. The usage endpoints
don't need either — the domain ``UsageStats`` *is* the aggregate the
consumer wants. The only HTTP-specific addition is the echoed
``from``/``to`` query window, so this module holds two thin wrappers
that add those fields and nothing else.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from qfa.domain.models import UsageStats


class UsageStatsResponse(UsageStats):
    """Domain ``UsageStats`` plus echoed ``from``/``to`` query bounds."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None


class AllUsageStatsResponse(BaseModel):
    """Per-tenant + grand total usage with optional echoed time window."""

    model_config = ConfigDict(populate_by_name=True)

    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    tenants: list[UsageStats]
    total: UsageStats
