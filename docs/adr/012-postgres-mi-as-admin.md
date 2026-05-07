# ADR-012: App Service Managed Identity as PostgreSQL Entra Admin

## Status

Accepted

## Context

The PostgreSQL Flexible Server is configured with Entra-only authentication
(`password_auth_enabled = false`). An Entra administrator must be set on the
server so the application can connect, migrations can run, and — in Entra's
model — the administrator role can grant further database roles.

Two main options were considered: make the App Service system-assigned
managed identity (MI) the Entra admin directly, or create an Entra group
whose members include the MI, and assign the group as admin.

## Decision

Assign the App Service system-assigned MI directly as the Entra
administrator (`azurerm_postgresql_flexible_server_active_directory_administrator`
points at `azurerm_linux_web_app.backend.identity[0].principal_id`).

The same MI is the runtime identity the app uses to connect to Postgres and
run migrations via `python -m qfa.cli.migrate`.

## Options Considered

### Option A: Dedicated Entra group as admin (rejected for now)

An Entra group (e.g. `qfa-dev-db-admins`) is set as the Postgres admin.
The MI is added to the group; individual humans can also be added for
ad-hoc debugging.

- **Pro**: Satisfies least-privilege — the app MI holds only the rights it
  needs; a separate, tightly-scoped human-admin role can be granted
  temporarily.
- **Pro**: Multiple principals (MI + on-call humans) can be admins without
  changing Terraform.
- **Con**: Requires provisioning the Entra group and managing group
  membership outside Terraform (or with an additional `azuread_group`
  resource and careful bootstrapping).
- **Con**: Increases operational complexity for a schema that currently
  stores only re-derivable token-count metadata with no PII.
- **Con**: An Entra group admin does not auto-provision its own Postgres
  role on first connect in the way an MI admin does; a manual
  `pgaadauth_create_principal` call or an Azure-managed bootstrap step
  is required.

### Option B: MI as direct admin (chosen)

The App Service MI is both the Entra admin on the server and the runtime
identity.

- **Pro**: Zero extra Entra resources — no group to create, no membership
  to manage.
- **Pro**: Azure auto-provisions the MI's Postgres role on its first
  connection when it is the Entra admin; no bootstrap SQL is required.
- **Pro**: Migrations (`python -m qfa.cli.migrate`) run under the same
  identity and with the same role the app uses at runtime — no
  discrepancy between migration time and run time.
- **Con**: The app MI holds elevated (admin) Postgres rights for the
  duration of its existence. This is acceptable while the schema stores
  only operational metadata (token counts, durations, costs) with no
  feedback content or PII. If PII is ever introduced, revisit.
- **Con**: No human principal can log in to Postgres directly without
  being added as a separate Entra admin or having a temporary password
  re-enabled (which requires a Terraform change). The documented
  workaround is `az webapp ssh` into the running container.

## Consequences

- `postgres.tf` assigns `azurerm_linux_web_app.backend.identity[0].principal_id`
  as the Postgres Entra admin.
- The application and the migration CLI both connect under the same MI
  identity; no separate migration-time credential is needed.
- An on-call engineer who needs direct Postgres access should use the
  `az webapp ssh` path documented in
  [setup-new-env.md § Debugging database connectivity](../../infra/setup-new-env.md#debugging-database-connectivity).
- If the schema ever stores PII or feedback content, reconsider Option A:
  create a dedicated Entra group for human admins, demote the app MI to
  a least-privileged role (INSERT/SELECT on the relevant tables), and
  track the group membership change in a follow-up ADR.

## When to revisit

- The schema is extended to store feedback content, PII, or any data
  that raises the data-protection bar beyond operational metadata.
- A second application or team needs independent write access to the
  database (makes the group approach worth the extra complexity).

## Participants

teeuwski, mariushelf
