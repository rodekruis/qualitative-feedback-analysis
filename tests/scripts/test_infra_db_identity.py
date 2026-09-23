"""Guard the Postgres Entra admin against being re-bound to the App Service (#177).

The App Service system-assigned principal ID is regenerated whenever the app is
recreated, but the in-database role is bound to the *old* ID — so an apply that
points the Entra administrator back at ``azurerm_linux_web_app.backend`` locks
the app out of its own database at exactly the moment it is being rebuilt. The
failure is silent in CI: nothing else here parses HCL, and `terraform plan`
succeeds. These assertions are the only thing standing between a well-meant
simplification and ADR-023 being undone.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
POSTGRES_TF = REPO_ROOT / "infra" / "postgres.tf"
APP_SERVICE_TF = REPO_ROOT / "infra" / "app_service.tf"


def _block(text: str, header: str) -> str:
    """Return the source of the top-level block starting with ``header``."""
    start = text.index(header)
    depth = 0
    for offset, char in enumerate(text[start:], start=start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : offset + 1]
    raise AssertionError(f"unterminated block {header!r}")


def test_dedicated_db_admin_identity_exists_and_is_protected():
    identity = _block(
        POSTGRES_TF.read_text(), 'resource "azurerm_user_assigned_identity" "db_admin"'
    )
    assert "local.db_identity_name" in identity, (
        f"the db_admin identity in {POSTGRES_TF} must take its name from "
        "local.db_identity_name — infra/app_service.tf derives DB_USER from the "
        "same local, and the two must never drift"
    )
    assert re.search(r"prevent_destroy\s*=\s*true", identity), (
        f"the db_admin identity in {POSTGRES_TF} must keep prevent_destroy — "
        "destroying it drops the principal ID the in-database role is bound to, "
        "and the grant then has to be rebuilt by hand (see ADR-023)"
    )


def test_entra_admin_points_at_the_dedicated_identity():
    admin = _block(
        POSTGRES_TF.read_text(),
        'resource "azurerm_postgresql_flexible_server_active_directory_administrator" "db"',
    )
    assert "azurerm_user_assigned_identity.db_admin.principal_id" in admin, (
        f"the Postgres Entra admin in {POSTGRES_TF} must be the dedicated "
        "user-assigned identity (ADR-023)"
    )
    assert "azurerm_linux_web_app" not in admin, (
        f"the Postgres Entra admin in {POSTGRES_TF} must not reference the App "
        "Service — its system-assigned principal ID changes on every rebuild, "
        "which is issue #177"
    )


def test_app_service_attaches_the_db_identity_and_names_it_in_settings():
    web_app = _block(
        APP_SERVICE_TF.read_text(), 'resource "azurerm_linux_web_app" "backend"'
    )
    identity = _block(web_app, "identity {")
    assert 'type         = "SystemAssigned, UserAssigned"' in identity, (
        f"the App Service in {APP_SERVICE_TF} must keep its system-assigned "
        "identity (Key Vault references, ACR pull) alongside the user-assigned one"
    )
    assert "azurerm_user_assigned_identity.db_admin.id" in identity, (
        f"the App Service in {APP_SERVICE_TF} must have the db_admin identity "
        "attached, or it cannot acquire a token as that principal"
    )
    assert (
        "DB_AAD_CLIENT_ID = azurerm_user_assigned_identity.db_admin.client_id"
        in web_app
    ), (
        f"{APP_SERVICE_TF} must set DB_AAD_CLIENT_ID — without it the app "
        "authenticates as the system-assigned identity while DB_USER names the "
        "user-assigned one, and every connection is rejected"
    )
    admin_ref = "azurerm_postgresql_flexible_server_active_directory_administrator.db"
    assert admin_ref in web_app, (
        f"the App Service in {APP_SERVICE_TF} must depend_on the Entra admin — "
        "entrypoint.sh runs migrations on first boot, before which the admin "
        "must exist"
    )


def test_role_assignments_stay_on_the_system_assigned_principal():
    text = APP_SERVICE_TF.read_text()
    for name in ("app_keyvault_secrets", "app_acr_repository_reader"):
        block = _block(text, f'resource "azurerm_role_assignment" "{name}"')
        assert "identity[0].principal_id" in block, (
            f"{name} must stay on the App Service system-assigned principal — "
            "Key Vault references and ACR pull both default to it, and moving "
            "only the assignment would break both (ADR-023)"
        )
        assert re.search(r"create_before_destroy\s*=\s*true", block), (
            f"{name} must set create_before_destroy — the assignment is replaced "
            "whenever the App Service is recreated, and destroying it first "
            "leaves the new app unable to read secrets or pull its image"
        )
