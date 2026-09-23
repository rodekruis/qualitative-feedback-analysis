Plan written. Here it is:

---

# Implementation plan — #177 Postgres Entra admin is the App Service system-assigned identity

## Scope note

`spec/issue-177.md` in this worktree has Context / Problem / Suggested fix but **no acceptance-criteria checklist**. I planned against the three "Suggested fix" bullets, treating them as the criteria: (1) dedicated user-assigned identity as DB admin, (2) document/script the DB-side re-grant, (3) explicit ordering on the role assignments. Everything the spec names exists in this tree (`infra/postgres.tf`, `infra/app_service.tf`, the "re-run steps 4 and 5" runbook in `docs/operations/setup-new-env.md`), so the work is buildable as written.

## Decisions the spec left open

| Question | Decision | Why |
|---|---|---|
| Which identity does the app keep for Key Vault + ACR? | **System-assigned, unchanged.** The web app becomes `type = "SystemAssigned, UserAssigned"`; only the DB uses the new UAMI. | Smallest blast radius. `azurerm_linux_web_app.identity[0].principal_id` still resolves to the *system-assigned* principal when the type includes `SystemAssigned`, so `infra/app_service.tf:124` and `:132` keep working untouched. Azure's `keyVaultReferenceIdentity` and ACR-pull identity both default to system-assigned, so KV references and image pulls are unaffected. Moving those to the UAMI too would mean setting `key_vault_reference_identity_id` and `container_registry_managed_identity_client_id` and risking a failed image pull on the same apply — not worth coupling to this fix. |
| UAMI name | `qfa-${env}-db-admin` | Must not collide with the Postgres server name (`qfa-${env}-db`) or the App Service name (`qfa-${env}-backend`). The in-DB role takes this name, so it must be readable in `psql` output. |
| Reuse the name `qfa-${env}-backend` for the UAMI so the existing in-DB role is inherited? | **Rejected.** | Would need a hand-written `SECURITY LABEL FOR "pgaadauth" ON ROLE ...` to re-point the role's OID, and would leave two Entra service principals sharing a display name. A distinct name plus a one-time re-grant (which the spec asks for anyway) is clearer. |
| How does the app pick the UAMI when acquiring the AAD token? | **New `DB_AAD_CLIENT_ID` setting** → `DefaultAzureCredential(managed_identity_client_id=...)`. Not the global `AZURE_CLIENT_ID` app setting. | `AZURE_CLIENT_ID` changes credential resolution for every Azure SDK client in the process, present and future. A `DB_`-prefixed field is explicit, sits in the existing `DatabaseSettings` group (ADR-006), and is unit-testable. Empty default = today's behaviour, so local dev and `password` mode are untouched. |
| How is the DB-side re-grant delivered? | **A CLI module, `python -m qfa.cli.db_grant`**, shipped in the image next to `qfa.cli.migrate`. | The DB has `public_network_access_enabled = false`, so the only way in is `az webapp ssh` into the container. The runtime image (see `Dockerfile`) copies `src/` and `alembic/` only — **not** `scripts/` — and contains no `psql` and no `az`. A `.sql` file in `scripts/` would be unreachable at the moment it's needed; a CLI in `src/qfa/cli/` is present, uses the asyncpg/azure-identity already installed, and can be tested with pytest. |
| Replace the Entra admin in one apply, or run two admins during cutover? | **One admin in Terraform**; the cutover safety comes from running `db_grant` *before* the apply, while the old admin still works. | Pre-creating the new role with its OID and full privileges means the swap needs no second admin and no window where nobody can reach the DB. |
| `skip_service_principal_aad_check` for the AAD-consistency flakiness the spec mentions | Added to the two app role assignments, in its own commit/step so it can be dropped in review. | It is the standard fix for `PrincipalNotFound` on first apply. Called out separately because it can also mask a genuinely wrong principal ID. |
| ADR handling | **Amend ADR-012 in place, add ADR-023.** Do not move 012 to `docs/adr/obsolete/`. | 012's group-vs-MI analysis is still the live reasoning; only the *which* MI part changes. The index already has the "Accepted (amended …)" precedent (ADR-003) and the "supersedes decision N of" precedent (ADR-017). Moving the file would break `docs/architecture-review-2026-05-08-addendum.md:182`. |

## Ordering constraints (across the whole change)

1. **Step 1 (UAMI resource) before steps 2–4** — they reference it.
2. **Step 5 (`DB_AAD_CLIENT_ID` setting) before step 3** writes that app setting — or, more precisely, both must land in the same PR: the app setting is inert until the Python side reads it, and the Python side is inert until the app setting exists. They must not ship in separate releases in either order, because between them the app would authenticate as the system-assigned identity while `DB_USER` names the UAMI role.
3. **`terraform apply` must be preceded by the `db_grant` run** for every existing environment (dev, staging, prd). This is an operational ordering constraint the code cannot enforce; it is why step 8 exists. Put it in the PR body so the human who runs apply sees it.
4. Steps 6–10 (tests, docs, ADR) have no ordering between them.

---

## Step 1 — Create the dedicated DB admin identity

**Files:** `infra/locals.tf`, `infra/postgres.tf`, `infra/outputs.tf`

1. In `infra/locals.tf`, replace line 9's `db_aad_principal_name = local.app_name # system-assigned MI name matches the App Service name` with:
   - `db_identity_name = "qfa-${local.env}-db-admin"`
   - `db_aad_principal_name = local.db_identity_name` — keep this local name, it is referenced from both `postgres.tf` and `app_service.tf`; only its value and comment change. The comment must now say the Postgres role name is the user-assigned identity's name, and why the identity is separate from the App Service (one sentence, link ADR-023 — do not restate it).
2. In `infra/postgres.tf`, immediately above `azurerm_postgresql_flexible_server_active_directory_administrator.db`, add:
   ```hcl
   resource "azurerm_user_assigned_identity" "db_admin" {
     name                = local.db_identity_name
     resource_group_name = data.azurerm_resource_group.main.name
     location            = data.azurerm_resource_group.main.location

     lifecycle {
       prevent_destroy = true
     }
   }
   ```
   `prevent_destroy` matches `azurerm_postgresql_flexible_server.db` and the private DNS zone in the same file: the whole point of this identity is that its principal ID outlives every App Service rebuild, so a `terraform destroy` must not silently take it.
3. In `infra/outputs.tf`, add two outputs — the runbook in step 8 reads both:
   - `postgres_admin_identity_name` → `azurerm_user_assigned_identity.db_admin.name`
   - `postgres_admin_identity_principal_id` → `azurerm_user_assigned_identity.db_admin.principal_id` (this is the object ID `pgaadauth_create_principal_with_oid` needs)

**Proven by:** step 7's `tests/scripts/test_infra_db_identity.py`.

## Step 2 — Point the Entra admin at the new identity

**File:** `infra/postgres.tf` (lines 65–72)

Change `object_id` from `azurerm_linux_web_app.backend.identity[0].principal_id` to `azurerm_user_assigned_identity.db_admin.principal_id`. `principal_name` stays `local.db_aad_principal_name` (now the UAMI name) and `principal_type` stays `"ServicePrincipal"`.

This removes the `azurerm_linux_web_app.backend` → admin dependency edge, which is what makes step 4 possible.

**Proven by:** step 7's test asserts the admin's `object_id` references `azurerm_user_assigned_identity.db_admin` and contains no `azurerm_linux_web_app` reference.

## Step 3 — Attach the identity to the App Service and tell the app to use it

**File:** `infra/app_service.tf`

1. Line 37–39 identity block →
   ```hcl
   identity {
     type         = "SystemAssigned, UserAssigned"
     identity_ids = [azurerm_user_assigned_identity.db_admin.id]
   }
   ```
   Add a short comment: system-assigned still serves Key Vault references and ACR pull (`identity[0].principal_id` below); the user-assigned one exists only for Postgres. This is an in-place update — it does **not** replace the web app and does **not** regenerate the system-assigned principal.
2. In `app_settings`, next to `DB_AAD_SCOPE` (line 73), add
   `DB_AAD_CLIENT_ID = azurerm_user_assigned_identity.db_admin.client_id`.
   `DB_USER` (line 74) needs no edit — it already reads `local.db_aad_principal_name`, whose value changed in step 1.
3. Leave lines 124 and 132 (`app_keyvault_secrets`, `app_acr_repository_reader`) pointing at `identity[0].principal_id`.

**Proven by:** step 7's test (identity type, `identity_ids`, `DB_AAD_CLIENT_ID` present and sourced from the UAMI, KV/ACR assignments still on `identity[0]`).

## Step 4 — Explicit ordering

**Files:** `infra/app_service.tf`, `infra/postgres.tf`

1. On `azurerm_linux_web_app.backend`, add
   ```hcl
   depends_on = [azurerm_postgresql_flexible_server_active_directory_administrator.db]
   ```
   with a comment explaining it: the container's `entrypoint.sh` runs `python -m qfa.cli.migrate` on first boot, so on a fresh environment the Entra admin must exist before the app is created or the first boot crash-loops on an unauthorised DB. This edge was impossible while the admin depended on the app; step 2 reversed it. **Verify no cycle**: the admin depends on `azurerm_postgresql_flexible_server.db` and `azurerm_user_assigned_identity.db_admin`; the web app depends on both of those plus the admin. No cycle.
2. On `azurerm_role_assignment.app_keyvault_secrets` and `azurerm_role_assignment.app_acr_repository_reader`, add
   ```hcl
   lifecycle {
     create_before_destroy = true
   }
   ```
   Comment: when the App Service is recreated its system-assigned principal changes and both assignments are replaced; without this, Terraform destroys the old assignment first, leaving a window where the new app cannot resolve Key Vault references or pull its image. Provider-generated GUID names mean there is no name collision during the overlap.
3. Separately (drop this sub-step if review objects — see decision table): add `skip_service_principal_aad_check = true` to the same two assignments, commented as the mitigation for the `PrincipalNotFound` failures on a first apply caused by Entra replication lag.

**Proven by:** step 7's test asserts the `depends_on` edge and the two `create_before_destroy` blocks.

## Step 5 — Application: acquire the token as the user-assigned identity

**Files:** `src/qfa/settings.py`, `src/qfa/adapters/db.py`

1. `DatabaseSettings` (around line 358, next to `aad_scope`): add
   `aad_client_id: str = ""`.
   One docstring/comment line stating the added fact: client ID of the user-assigned managed identity to authenticate as; empty means let `DefaultAzureCredential` choose (system-assigned MI, or the developer's `az login` locally). Ignored outside `entra` mode. No validator change — empty is valid in both modes.
2. `_AadTokenProvider.__init__` (`src/qfa/adapters/db.py:147`): take `client_id: str | None = None` and build
   `DefaultAzureCredential(managed_identity_client_id=client_id)` when it is truthy, plain `DefaultAzureCredential()` otherwise. Do not pass `managed_identity_client_id=None` explicitly if that differs in behaviour for the installed `azure-identity` — branch instead.
3. `create_async_engine_from_settings` (line ~193): pass `settings.aad_client_id or None` into `_AadTokenProvider`. Nothing else in that function changes; `resolve_database_url` is untouched.

**Proven by:** step 6.

## Step 6 — Tests for the application change

**Files:** `tests/test_settings.py`, `tests/adapters/test_db.py`

1. `tests/test_settings.py`, `TestDatabaseSettings` (the class holding `test_accepts_entra_mode_without_password`, ~line 252): two tests — `aad_client_id` defaults to `""` when `DB_AAD_CLIENT_ID` is unset; it reads the env var when set. Follow the existing `monkeypatch.delenv/setenv` style in that class.
2. `tests/adapters/test_db.py`: two tests around `create_async_engine_from_settings`, monkeypatching `qfa.adapters.db.DefaultAzureCredential` with a recorder:
   - entra mode + `aad_client_id="11111111-..."` → the credential is constructed with `managed_identity_client_id` equal to that value.
   - entra mode + empty `aad_client_id` → constructed with no `managed_identity_client_id` (regression guard for local dev / system-assigned).
   Keep them in the same style as the existing `test_resolve_database_url_from_entra_parts` (~line 271). Do not open a real connection — asserting on construction is enough; the `do_connect` hook is already exercised elsewhere.

**Run:** `make test` and `make lint` (ruff + ty + `lint-imports`).

## Step 7 — Test for the Terraform change

**File:** `tests/scripts/test_infra_db_identity.py` (new)

`terraform` is **not installed in this worktree**, so the builder cannot run `terraform validate` or `fmt` locally. Use the repo's existing pattern for testing HCL: `tests/scripts/test_infra_judge_config.py` regex-parses `infra/variables.tf`. Mirror it — module docstring explaining what silent failure the test prevents (an apply that re-binds the Postgres admin to the App Service system-assigned principal reintroduces #177, and nothing else in CI would catch it), then:

1. `azurerm_user_assigned_identity "db_admin"` exists in `infra/postgres.tf` and carries `prevent_destroy = true`.
2. The `azurerm_postgresql_flexible_server_active_directory_administrator "db"` block references `azurerm_user_assigned_identity.db_admin.principal_id` and does **not** contain the substring `azurerm_linux_web_app`.
3. `infra/app_service.tf`'s `identity` block has `type = "SystemAssigned, UserAssigned"` and lists `azurerm_user_assigned_identity.db_admin.id` in `identity_ids`.
4. `app_settings` contains `DB_AAD_CLIENT_ID` sourced from `azurerm_user_assigned_identity.db_admin.client_id`.
5. Both `azurerm_role_assignment` blocks in `app_service.tf` still use `identity[0].principal_id` (guards the KV/ACR decision) and both declare `create_before_destroy = true`.
6. `azurerm_linux_web_app "backend"` declares `depends_on` on the Entra administrator resource.

Each assertion gets a failure message naming the file and what to do, matching the existing file's tone. Write the regexes against the real file contents — check them by running the test, not by eye.

## Step 8 — The re-grant CLI

**Files:** `src/qfa/cli/db_grant.py` (new), `tests/adapters/` or `tests/scripts/` → put its test at `tests/test_db_grant.py` next to the CLI's peers (match wherever `qfa.cli.migrate` is currently tested; if it has no test, use `tests/adapters/test_db_grant.py`).

Purpose: run **as the current Entra admin** from inside the container, and provision a *different* principal as a Postgres admin with full access to what the current admin owns. This is the "DB-side re-grant" the spec asks to script. It serves two situations: the one-time cutover (step 9's runbook) and the future case where the DB admin identity itself is ever recreated.

Shape:
- `python -m qfa.cli.db_grant --principal-name <name> --object-id <oid> [--from-role <existing-role>]`
- Builds settings via `DatabaseSettings()` and an engine via `create_async_engine_from_settings` — i.e. it connects with whatever identity the container currently has. Reuse `qfa.adapters.db`, do not open a second connection path.
- Statements, in order, each logged:
  1. Idempotency check — `SELECT 1 FROM pg_roles WHERE rolname = :name`; skip creation if present.
  2. `SELECT pgaadauth_create_principal_with_oid(:name, :oid, 'service', true, false)` — `true` = admin, so the new role gets `azure_pg_admin` regardless of the server-level admin assignment.
  3. When `--from-role` is given: `GRANT "<from-role>" TO "<name>"` then `REASSIGN OWNED BY "<from-role>" TO "<name>"` — the first makes existing objects reachable immediately, the second transfers ownership so the old role can eventually be dropped.
  4. `GRANT ALL ON SCHEMA public TO "<name>"`.
- Identifiers cannot be bound as parameters — quote them with `psycopg`/`asyncpg`-safe quoting or a strict `^[A-Za-z0-9-]+$` validation on `--principal-name`/`--from-role` that raises before any SQL is built. Do not string-format an unvalidated identifier into SQL.
- Exit non-zero with a readable message on failure; no silent success.
- Module docstring: what it does, who must be connected to run it (the current Entra admin), and that it is idempotent.

**Proven by:** tests against a fake/stubbed engine asserting (a) the exact statement sequence for a fresh principal, (b) creation is skipped when the role already exists but grants still run, (c) an invalid identifier raises before any SQL is emitted. A live-Postgres integration test is out of scope — `pgaadauth_create_principal_with_oid` is Azure-only and does not exist in the `postgres:16` service container CI uses.

## Step 9 — Documentation

All in the same PR (AGENTS.md). Keep each edit to the added fact; do not restate the ADR in the runbooks.

1. **`docs/adr/023-dedicated-identity-as-postgres-admin.md`** (new) — follow the house structure (Status / Context / Decision / Options Considered / Consequences / When to revisit / Participants). Decision: a dedicated user-assigned identity is the Postgres Entra admin and the app's DB credential; the system-assigned identity keeps Key Vault and ACR. Options considered must include (a) system-assigned as admin — the status quo and its failure mode, (b) dedicated UAMI (chosen), (c) Entra group, cross-referencing ADR-012's Option A rather than re-arguing it. Consequences: existing environments need the one-time re-grant; `terraform destroy` on the identity is blocked by `prevent_destroy`; the app now needs `DB_AAD_CLIENT_ID`. Participants: leave as the issue's participants.
2. **`docs/adr/012-postgres-mi-as-admin.md`** — Status → `Accepted (amended 2026-09-23 — the admin is a dedicated user-assigned identity, see ADR-023)`, plus a short amendment note. Do not rewrite the body.
3. **`docs/adr/index.md`** — new row for 023, updated Status cell for 012, and `023-dedicated-identity-as-postgres-admin` added to the hidden toctree (the Sphinx build runs with `-W`; a page missing from a toctree fails it).
4. **`docs/operations/setup-new-env.md`** —
   - Step 2's line "PostgreSQL Entra admin is configured automatically from the App Service system-assigned managed identity." is now wrong; replace with the dedicated identity.
   - "Re-running after `terraform destroy`" — this is the section the issue calls out as misleading. Rewrite: `AZ_CLIENT_ID` refresh is unchanged, but state plainly that recreating the App Service no longer affects DB access, and that if the **DB admin identity** is recreated the in-database grant must be re-established — link to the new how-to.
   - "Debugging database connectivity" — the `psql` recipe there cannot work: the runtime image (`Dockerfile`) has no `psql` and no `az`. Replace it with a `.venv/bin/python` one-liner that uses the installed `azure-identity` + the app's own settings, or mark the step as requiring a client installed in the session. Flag this in the PR as a pre-existing bug found while doing #177 — it is in scope because the same section is the entry point for the new runbook.
5. **`docs/operations/how-to.md`** — new section, in the file's copy-pasteable style: **"Cut over an existing environment to the dedicated DB identity"**, the one-time migration, in the order it must actually run:
   1. `terraform apply -target=azurerm_user_assigned_identity.db_admin` — creates the identity only, nothing else moves.
   2. `terraform output -raw postgres_admin_identity_name` / `postgres_admin_identity_principal_id`.
   3. `az webapp ssh` into the still-working app and run `.venv/bin/python -m qfa.cli.db_grant --principal-name <name> --object-id <oid> --from-role qfa-<env>-backend`.
   4. Full `terraform apply` — swaps the server's Entra admin and the app settings; the container restarts and `qfa.cli.migrate` runs as the new principal against a role that already has every privilege.
   5. Verify: `GET /v1/health`, and the container log shows migrations completing rather than an auth error.
   Include the rollback: re-point `object_id` at `azurerm_linux_web_app.backend.identity[0].principal_id` and re-apply — the old role still exists and still owns nothing it lost, because step 3 granted rather than revoked.
   Add a second short section **"The DB admin identity was recreated"** covering the case where no working admin remains: add a human as a temporary Entra admin with `az postgres flexible-server ad-admin create`, then run `db_grant` from a VNet-attached session.
6. **`docs/operations/settings-reference.md`** — add a `DB_AAD_CLIENT_ID` row to the `DB_*` table (after `DB_AAD_SCOPE`, line 178): not required, default `""`, note that it selects a user-assigned managed identity in `entra` mode and that empty means the default credential chain. Update the `DB_USER` row's note — for `entra` mode it is now the DB admin identity's name, not the App Service's.
7. **`docs/operations/deployment.md`** — "Database authentication" section: the `entra` bullet says "The App Service system-assigned managed identity must be granted the PostgreSQL role"; correct it to the dedicated user-assigned identity and link ADR-023. In "Recovering from common situations", the "Managed identity recreated" bullet needs the same correction as setup-new-env.md, pointing at the new how-to.
8. **`scripts/README.md`** — no change; `db_grant` is a `qfa.cli` module, not a `scripts/` entry. Instead make sure it is reachable from the docs above.
9. **`docs/security-brief.html`** — read line ~315 ("Services authenticate to each other via **managed identity** — no shared-key secrets between components"). It stays accurate under this change; **make no edit**, and say so explicitly in the PR body so the reviewer knows the AGENTS.md security-doc rule was considered rather than missed.

**Proven by:** `make docs` (Sphinx runs `-W --keep-going`, so a missing toctree entry or a broken cross-ref fails the build).

## Step 10 — PR

One PR, `closes #177`. Conventional commits, subject line + body only, no trailers. The PR body must carry three things the reviewer cannot infer from the diff:

- **The apply is not safe to run from the Actions tab without the cutover runbook.** Every existing environment (dev, staging, prd) needs `db_grant` run against it *before* `terraform apply`, one environment at a time.
- **What the plan will show**: the Entra administrator resource **replaced**, the web app **updated in place** (identity type + two app settings), a new user-assigned identity **created**, and the two role assignments showing no change. If the plan shows the web app being *replaced*, stop — that would regenerate the system-assigned principal and break Key Vault and ACR access.
- `make test`, `make lint`, `make docs` results, and the note that `terraform fmt`/`validate` could not be run locally (no binary in the dev container), so the `Terraform / terraform` PR check is the only HCL validation — it must be green before merge.

Request a review; the "Review by at least one other human" ruleset means self-approval does not count.

## Out of scope (state in the PR, do not build)

- Moving Key Vault references and ACR pull to the user-assigned identity.
- An Entra *group* as admin (ADR-012 Option A) — unchanged, still rejected.
- Dropping the old `qfa-<env>-backend` Postgres role after cutover. `REASSIGN OWNED BY` makes it droppable, but dropping it is a separate, irreversible operator action; document it in the how-to as optional, do not automate it.
- A `time_sleep`/`hashicorp/time` provider for Entra replication lag. `skip_service_principal_aad_check` (step 4.3) covers the role assignments; the identity itself is long-lived, so the lag only ever affects a first apply, where a retry is the documented answer.
