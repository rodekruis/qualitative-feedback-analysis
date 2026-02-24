# ADR-005: Bearer Token Authentication

## Status

Accepted

## Context

The API must authenticate requests using API keys. Each CRM tenant has one
or more named keys for rotation support. The question is which HTTP header
convention to use.

## Decision

Use `Authorization: Bearer <key>` (RFC 6750).

## Options Considered

### Option A: X-API-Key custom header (rejected)

- **Pro**: Used by many API providers (Stripe, SendGrid). Visually distinct
  from OAuth bearer tokens, which may reduce confusion in environments that
  also use OAuth.
- **Con**: Non-standard. Many API gateways, load balancers, and HTTP client
  libraries have first-class support for `Authorization: Bearer` but require
  custom configuration for `X-API-Key`. CRM developers familiar with OAuth2
  workflows will expect `Authorization: Bearer` and may lose time debugging
  when their default HTTP client configuration sends the wrong header.

### Option B: Authorization: Bearer (chosen)

- **Pro**: RFC 6750 standard. Universal tooling support. CRM developers can
  use their existing HTTP client configuration without modification. Azure
  API Management, AWS API Gateway, and Kong all support Bearer tokens natively.
- **Con**: May be confused with OAuth2 bearer tokens. A CRM developer might
  assume token exchange or refresh is supported.
- **Mitigation**: The API documentation and 401 error message explicitly
  state that the token is a static API key, not an OAuth token. No token
  exchange endpoint exists.

### Option C: Basic auth (not considered)

Would require Base64 encoding and a username:password pair. Unnecessary
complexity for a static API key.

## Consequences

- The `authenticate_request` dependency reads the `Authorization` header
  and extracts the token after the `Bearer ` prefix.
- FastAPI's `HTTPBearer` security scheme is used, which auto-generates the
  correct OpenAPI security definition.
- 401 responses include the message:
  `"A valid API key is required. Provide it as: Authorization: Bearer <key>"`
- Key validation uses `secrets.compare_digest` for constant-time comparison.

## Participants

- UX advocate (proposed Bearer as RFC standard)
- Architect (originally proposed X-API-Key, accepted Bearer)
