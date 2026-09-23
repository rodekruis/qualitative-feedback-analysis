# ADR-023: Dedicated user-assigned identity as PostgreSQL Entra admin

## Status

Accepted

## Context

[ADR-012](012-postgres-mi-as-admin.md) made the App Service *system-assigned*
managed identity the Postgres Entra administrator and the app's runtime DB
credential. A system-assigned principal exists only as long as the App Service
does: recreate the app and Azure mints a new principal ID.

The in-database role does not follow. Azure Postgres binds a role to the
principal's object ID, so after a rebuild the role in `pg_roles` still points at
the identity that no longer exists. Every connection is rejected, the
entrypoint's `python -m qfa.cli.migrate` fails, and the container restart-loops —
during a rebuild, which is exactly when the environment is least understood
(#177).

The documented recovery ("re-run steps 4 and 5") refreshes `AZ_CLIENT_ID` for
GitHub Actions. It has no effect on the in-database grant, so it reads as a fix
and is not one.

A second, smaller symptom shares the cause: the admin resource depended on the
web app, so on a first apply the app was created before it had any database
rights, and Entra replication lag made the dependent role assignments fail
intermittently with `PrincipalNotFound`.

## Decision

1. **A dedicated user-assigned managed identity, `qfa-<env>-db-admin`, is the
   Postgres Entra administrator** (`azurerm_user_assigned_identity.db_admin` in
   `infra/postgres.tf`). Its principal ID is a property of the identity, not of
   the App Service, so it survives every App Service rebuild. The resource
   carries `prevent_destroy`.
2. **The App Service keeps its system-assigned identity** and gains the
   user-assigned one (`type = "SystemAssigned, UserAssigned"`). Key Vault
   references and the ACR pull both default to the system-assigned principal and
   stay on it; only Postgres uses the new identity.
3. **The app selects it explicitly** via `DB_AAD_CLIENT_ID`, passed to
   `DefaultAzureCredential(managed_identity_client_id=...)`. A `DB_`-prefixed
   setting rather than the global `AZURE_CLIENT_ID`, which would redirect every
   Azure SDK client in the process. Empty is today's behaviour, so local dev and
   `password` mode are untouched.
4. **The re-grant is a shipped CLI**, `python -m qfa.cli.db_grant`, not a
   `.sql` file under `scripts/`. The database has no public network access, so
   the only way in is `az webapp ssh`; the runtime image contains `src/` and
   `alembic/` but no `psql`, no `az`, and no `scripts/`.
5. **Ordering is explicit.** The web app `depends_on` the Entra administrator
   (migrations run on first boot); the two role assignments are
   `create_before_destroy` and skip the provider's up-front principal check.

## Options Considered

### Option A: Entra group as admin (still rejected)

Unchanged from [ADR-012 § Option A](012-postgres-mi-as-admin.md#option-a-dedicated-entra-group-as-admin-rejected-for-now)
— the group has to be provisioned and its membership managed outside Terraform,
and it does not auto-provision its Postgres role either. A user-assigned
identity gives the stable principal ID this ADR needs without any of that.

### Option B: App Service system-assigned identity as admin (the status quo)

- **Pro**: zero extra resources; Azure auto-provisions the role on first connect.
- **Con**: the failure above. The lifetime of the credential is tied to the
  lifetime of the compute that uses it, which is the one thing that gets
  rebuilt.

### Option C: Dedicated user-assigned identity (chosen)

- **Pro**: stable principal ID; an App Service rebuild is now a no-op for the
  database.
- **Pro**: the admin no longer depends on the app, which frees the app to depend
  on the admin — the ordering migrations actually need.
- **Con**: an existing environment's tables stay owned by `qfa-<env>-backend`.
  Azure auto-provisions the new admin's own role on first connect, so it can log
  in. It owns nothing until the one-time transfer has run, and reads
  `permission denied for table ...` before then. A new environment needs nothing.
- **Con**: one more Entra principal per environment, and one more app setting.

### Option D: Reuse the name `qfa-<env>-backend` for the new identity

Rejected. The existing role would appear to be inherited but would still carry
the old object ID, so it would need a hand-written
`SECURITY LABEL FOR "pgaadauth" ON ROLE ...` to re-point — and two Entra service
principals would share a display name.

## Consequences

- Existing environments (`dev`, `staging`, `prd`) need the one-time cutover in
  [How-to § Cut over an existing environment to the dedicated DB identity](../operations/how-to.md#cut-over-an-existing-environment-to-the-dedicated-db-identity),
  run **before** `terraform apply`, one environment at a time.
- `DB_USER` is now the DB admin identity's name, not the App Service's. The old
  `qfa-<env>-backend` role keeps existing after cutover; dropping it is optional
  and manual.
- `terraform destroy` on an environment now stops on the identity's
  `prevent_destroy`, as it already does on the server and the DNS zone.
- The app reads one new setting, `DB_AAD_CLIENT_ID`. Terraform must not get
  ahead of the image: it would set `DB_USER` to the user-assigned identity while
  an older image still authenticates as the system-assigned one. The other order
  is inert, because the setting is unset until Terraform writes it, which is what
  makes step 1 of the cutover safe.

## When to revisit

- If Key Vault references or the ACR pull ever need to move to the user-assigned
  identity (`key_vault_reference_identity_id`,
  `container_registry_managed_identity_client_id`), which would let the App
  Service drop its system-assigned identity entirely.
- If a second principal (a human on-call role, another service) needs database
  access, which is the point where ADR-012's Option A starts paying for itself.

## Participants

Olaf
