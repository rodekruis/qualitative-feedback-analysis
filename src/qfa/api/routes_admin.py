"""API route handlers for auth-management endpoints."""

import secrets
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Response

from qfa.api.dependencies import get_auth_orchestrator, require_superuser
from qfa.api.schemas import (
    ApiAddKeyRequest,
    ApiAddKeyResponse,
    ApiAddTenantRequest,
    ApiAddTenantResponse,
    ApiAuthKey,
    ApiAuthKeysResponse,
    ApiTenant,
    ApiTenantsResponse,
)
from qfa.domain.models import TenantApiKey
from qfa.services.auth_orchestrator import AuthOrchestrator

router = APIRouter()


@router.post(
    "/v1/admin/tenants",
    response_model=ApiAddTenantResponse,
    status_code=201,
    tags=["User Management"],
)
async def add_tenant(
    body: ApiAddTenantRequest,
    _tenant: TenantApiKey = Depends(require_superuser),
    auth_orchestrator: AuthOrchestrator = Depends(get_auth_orchestrator),
) -> ApiAddTenantResponse:
    """Create a tenant. Requires superuser access."""
    tenant_id = await auth_orchestrator.add_tenant(
        tenant_name=body.tenant_name,
        allows_superusers=body.allows_superusers,
    )
    return ApiAddTenantResponse(tenant_id=tenant_id)


@router.get(
    "/v1/admin/tenants",
    response_model=ApiTenantsResponse,
    status_code=200,
    tags=["User Management"],
)
async def get_tenants(
    _tenant: TenantApiKey = Depends(require_superuser),
    auth_orchestrator: AuthOrchestrator = Depends(get_auth_orchestrator),
) -> ApiTenantsResponse:
    """List all tenants. Requires superuser access."""
    tenant_records = await auth_orchestrator.get_tenants()
    return ApiTenantsResponse(
        tenants=[
            ApiTenant(
                tenant_id=record["tenant_id"],
                name=record["name"],
                allows_superusers=record["allows_superusers"],
            )
            for record in tenant_records
        ]
    )


@router.delete(
    "/v1/admin/tenants/{tenant_id}", status_code=204, tags=["User Management"]
)
async def delete_tenant(
    tenant_id: str,
    _tenant: TenantApiKey = Depends(require_superuser),
    auth_orchestrator: AuthOrchestrator = Depends(get_auth_orchestrator),
) -> Response:
    """Delete a tenant and related keys. Requires superuser access."""
    await auth_orchestrator.delete_tenant(tenant_id)
    return Response(status_code=204)


@router.post(
    "/v1/admin/keys",
    response_model=ApiAddKeyResponse,
    status_code=201,
    tags=["User Management"],
)
async def add_key(
    body: ApiAddKeyRequest,
    _tenant: TenantApiKey = Depends(require_superuser),
    auth_orchestrator: AuthOrchestrator = Depends(get_auth_orchestrator),
) -> ApiAddKeyResponse:
    """Create an API key for a tenant. Requires superuser access."""
    key_id = str(uuid4())
    api_key = secrets.token_urlsafe(32)
    await auth_orchestrator.add_key(
        api_key=api_key,
        key_id=key_id,
        key_name=body.key_name,
        tenant_id=body.tenant_id,
        is_superuser=body.is_superuser,
    )
    return ApiAddKeyResponse(key_id=key_id, api_key=api_key)


@router.delete("/v1/admin/keys/{key_id}", status_code=204, tags=["User Management"])
async def delete_key(
    key_id: str,
    _tenant: TenantApiKey = Depends(require_superuser),
    auth_orchestrator: AuthOrchestrator = Depends(get_auth_orchestrator),
) -> Response:
    """Delete an API key by id. Requires superuser access."""
    await auth_orchestrator.delete_key(key_id)
    return Response(status_code=204)


@router.get(
    "/v1/admin/keys",
    response_model=ApiAuthKeysResponse,
    status_code=200,
    tags=["User Management"],
)
async def get_auth_keys(
    tenant_id: str | None = Query(
        default=None,
        description="Optional tenant id to filter keys.",
    ),
    _tenant: TenantApiKey = Depends(require_superuser),
    auth_orchestrator: AuthOrchestrator = Depends(get_auth_orchestrator),
) -> ApiAuthKeysResponse:
    """List API key metadata. Requires superuser access."""
    auth_keys = await auth_orchestrator.get_auth_keys(tenant_id)
    return ApiAuthKeysResponse(
        auth_keys=[
            ApiAuthKey(
                key_id=record["key_id"],
                name=record["name"],
                tenant_id=record["tenant_id"],
                is_superuser=record["is_superuser"],
            )
            for record in auth_keys
        ]
    )
