Context: the App Service authenticates to Postgres via Entra (no password auth); the DB's Entra administrator is wired in `postgres.tf`.

## Problem

`postgres.tf` binds the Postgres Entra admin to the App Service's **system-assigned** managed identity. A system-assigned principal ID is regenerated whenever the App Service is recreated. If the App Service is recreated out-of-band and then `terraform apply` runs, the in-database role still references the old principal ID → auth failures, failed migrations, container restart-loop — exactly during a rebuild. The documented recovery ("re-run steps 4 and 5") refreshes the OIDC client ID, not the in-DB grant. Azure AD eventual consistency also makes first applies flaky.

## Suggested fix

- Use a dedicated **user-assigned** managed identity for the DB admin (stable principal ID across recreates).
- Document/script the DB-side re-grant in the "identity recreated" runbook.
- Add explicit ordering (`depends_on` / `create_before_destroy`) on the role assignments.
