"""Unit tests for ``_translate_db_errors`` error translation (no DB needed).

Aggregation behaviour against real PostgreSQL lives in
``tests/integration/test_usage_repository.py``, excluded by the default
test selection (``-m 'not integration and not e2e'``) — the containment
assertions below need a plain unit test to run at all.
"""

import pytest
from sqlalchemy.exc import InterfaceError, OperationalError

from qfa.adapters.usage_repository import _translate_db_errors
from qfa.domain.errors import UsageRepositoryUnavailableError

SENTINEL = "LEAK-CANARY-7f3a"


class TestTranslateDbErrors:
    @pytest.mark.asyncio
    async def test_operational_error_sentinel_absent_from_domain_error(self):
        with pytest.raises(UsageRepositoryUnavailableError) as excinfo:
            async with _translate_db_errors():
                raise OperationalError(
                    "SELECT 1", {}, Exception(f"could not connect to {SENTINEL}")
                )

        err = excinfo.value
        assert SENTINEL not in str(err)
        assert not any(SENTINEL in str(a) for a in err.args)
        assert err.__cause__ is not None

    @pytest.mark.asyncio
    async def test_interface_error_sentinel_absent_from_domain_error(self):
        with pytest.raises(UsageRepositoryUnavailableError) as excinfo:
            async with _translate_db_errors():
                raise InterfaceError(
                    "SELECT 1", {}, Exception(f"could not connect to {SENTINEL}")
                )

        err = excinfo.value
        assert SENTINEL not in str(err)
        assert not any(SENTINEL in str(a) for a in err.args)
        assert err.__cause__ is not None
