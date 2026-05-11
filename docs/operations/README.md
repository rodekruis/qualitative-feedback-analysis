# Operations

Running, deploying, and observing the service.

| Doc | When you need it |
|---|---|
| [Deployment: runtime overview](deployment.md) | How the service runs — container, migrations, multi-replica safety |
| [Infrastructure bootstrap](bootstrap.md) | One-time setup of the shared Terraform backend and container registry |
| [Set up a new environment](setup-new-env.md) | Per-environment provisioning (`dev`, `staging`, `prd`) |
| [API key management](auth-management.md) | Adding, rotating, and revoking API keys |
| [Settings reference](settings-reference.md) | Every environment variable the app reads |
| [Observability](observability.md) | What gets logged, request tracing, usage queries |

For first-time setup, follow [Infrastructure bootstrap](bootstrap.md) → [Set up a new environment](setup-new-env.md) in that order.
