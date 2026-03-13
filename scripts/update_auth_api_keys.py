#!/usr/bin/env python3
"""Read auth-api-keys from Key Vault and update them, and write back.

Keys are stored in Key Vault as secrets as a json string, representing a list of :class:`TenantApiKey`.

Simple CLI script.

Usage:

update_auth_api_keys <tenant> <operation>

Operations:
--replace: Replace existing auth-api-keys for a tenant with a new one
--add: Add a new auth-api-key for a tenant
--remove: Remove all auth-api-keys for a tenant
--is-superuser (bool): if true, any new key will not have superuser privileges. If one, new key has superuser privileges. If omitted, and there is a key for the tenant, new key will use same superuser setting as existing key. If omitted and there is no existing key, do NOT grant superuser privileges. NOT IMPLEMENTED YET

Keys are generated with secrets.token_urlsafe(64)
"""

import argparse
import json
import logging
import os
import secrets
import sys

from azure.identity import AzureCliCredential
from azure.keyvault.secrets import SecretClient
from pydantic import SecretStr

from qfa.domain.models import TenantApiKey

SECRET_NAME = "AUTH-API-KEYS"  # noqa: S105 (not a password, it's the Key Vault secret name)

logger = logging.getLogger(__name__)


def _build_client() -> SecretClient:
    """Build a Key Vault SecretClient using Azure CLI credentials."""
    vault_name = os.environ.get("AZURE_KEYVAULT")
    if not vault_name:
        print("Error: AZURE_KEYVAULT environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    vault_url = f"https://{vault_name}.vault.azure.net"
    return SecretClient(vault_url=vault_url, credential=AzureCliCredential())


def _load_keys(client: SecretClient) -> list[TenantApiKey]:
    """Load existing API keys from Key Vault. Returns empty list if secret doesn't exist."""
    try:
        secret = client.get_secret(SECRET_NAME)
    except Exception:
        logger.info(
            "No existing secret '%s' found, starting with empty list.", SECRET_NAME
        )
        return []

    raw = secret.value
    if not raw:
        return []

    items = json.loads(raw)
    return [TenantApiKey(**item) for item in items]


def _save_keys(client: SecretClient, keys: list[TenantApiKey]) -> None:
    """Serialize keys and write them back to Key Vault."""
    payload = json.dumps(
        [
            {
                "key_id": k.key_id,
                "name": k.name,
                "key": k.key.get_secret_value(),
                "tenant_id": k.tenant_id,
            }
            for k in keys
        ]
    )
    client.set_secret(SECRET_NAME, payload)


def _generate_key() -> str:
    return secrets.token_urlsafe(64)


def _keys_for_tenant(keys: list[TenantApiKey], tenant: str) -> list[TenantApiKey]:
    return [k for k in keys if k.tenant_id == tenant]


def _keys_without_tenant(keys: list[TenantApiKey], tenant: str) -> list[TenantApiKey]:
    return [k for k in keys if k.tenant_id != tenant]


def _next_key_id(keys: list[TenantApiKey], tenant: str) -> str:
    """Generate the next key_id for a tenant (e.g. ``"tenant-0"``, ``"tenant-1"``)."""
    existing = _keys_for_tenant(keys, tenant)
    return f"{tenant}-{len(existing)}"


def add(
    keys: list[TenantApiKey], tenant: str
) -> tuple[list[TenantApiKey], TenantApiKey]:
    """Add a new API key for a tenant (keeps existing keys)."""
    key_id = _next_key_id(keys, tenant)
    new_key = TenantApiKey(
        key_id=key_id,
        name=key_id,
        key=SecretStr(_generate_key()),
        tenant_id=tenant,
    )
    return [*keys, new_key], new_key


def replace(
    keys: list[TenantApiKey], tenant: str
) -> tuple[list[TenantApiKey], TenantApiKey]:
    """Remove all existing keys for tenant and create one new key."""
    remaining = _keys_without_tenant(keys, tenant)
    key_id = f"{tenant}-0"
    new_key = TenantApiKey(
        key_id=key_id,
        name=key_id,
        key=SecretStr(_generate_key()),
        tenant_id=tenant,
    )
    return [*remaining, new_key], new_key


def remove(keys: list[TenantApiKey], tenant: str) -> list[TenantApiKey]:
    """Remove all API keys for a tenant."""
    return _keys_without_tenant(keys, tenant)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Manage tenant API keys in Azure Key Vault.",
    )
    parser.add_argument("tenant", help="Tenant identifier")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add", action="store_const", const="add", dest="operation")
    group.add_argument(
        "--replace", action="store_const", const="replace", dest="operation"
    )
    group.add_argument(
        "--remove", action="store_const", const="remove", dest="operation"
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Entry point."""
    logging.basicConfig(level=logging.INFO)
    args = parse_args(argv)
    tenant: str = args.tenant
    operation: str = args.operation

    client = _build_client()
    keys = _load_keys(client)

    existing = _keys_for_tenant(keys, tenant)
    if existing:
        print(f"Found {len(existing)} existing key(s) for tenant '{tenant}'.")
    else:
        print(f"No existing keys for tenant '{tenant}'.")

    if operation == "add":
        keys, new_key = add(keys, tenant)
        _save_keys(client, keys)
        print(f"Added key '{new_key.key_id}' for tenant '{tenant}'.")
        print(f"Key: {new_key.key.get_secret_value()}")

    elif operation == "replace":
        keys, new_key = replace(keys, tenant)
        _save_keys(client, keys)
        print(f"Replaced keys for tenant '{tenant}' with new key '{new_key.key_id}'.")
        print(f"Key: {new_key.key.get_secret_value()}")

    elif operation == "remove":
        keys = remove(keys, tenant)
        _save_keys(client, keys)
        print(f"Removed all keys for tenant '{tenant}'.")


if __name__ == "__main__":
    main()
