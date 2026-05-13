# 013 - Keys in DB and Environment Variables

## Status

Accepted

## Context

The application needs to manage API keys for tenants. Each tenant has a single API key that is used for authentication when they make requests to the API. The API key is a secret value that should be stored securely.
The main options for storing the API keys are:
1. Store the API keys in the database, encrypted at rest.
2. Store the API keys in environment variables, with one variable per tenant.

## Decision

API keys are stored in the database, and *also* in environment variables.

## Options Considered
### Option A: Store API keys in the database only (rejected)
- **Pro**: Centralized storage. Easier to manage at scale as the number of tenants grows.
- **Pro**: Admins can manage keys without needing access to the application environment.

- **Con**: Chicken-and-egg problem for authentication. If the app needs the API key to authenticate to the database, but the key is stored in the database, how does it authenticate on startup?

### Option B: Store API keys in environment variables only (rejected)
- **Pro**: Simple to implement. No need for encryption at rest since environment variables are not persisted in the same way as database records.
- **Pro**: No risk of database compromise exposing API keys.

- **Con**: Not scalable. As the number of tenants grows, managing environment variables becomes unwieldy.
- **Con**: Requires redeploying the application to add or rotate keys, which is operationally complex and error-prone.

### Option C: Store API keys in both the database and environment variables (chosen)
- **Pro**: The database serves as the source of truth for API keys, allowing for centralized management of users and keys. Environment variables provide a fallback for authentication on startup, solving the chicken-and-egg problem. This also allows other sevices (like a future dashboard) to use a key that's mentioned in the terraform variables without needing to query the database.

- **Con**: Introduces some complexity as we're now using two storage mechanisms for API keys. However, the benefits of scalability and operational simplicity outweigh this complexity.

## Consequences
- `AuthLookupPort` has two implementations: one that looks up API keys in the database, and another that looks them up in environment variables. 

## When to revisit

- 

## Participants

teeuwksi, mariushelf