# Security and Hardening Guidance

This document provides a high-level security posture and deployment guidance for QFA in sensitive humanitarian contexts.

## Scope and threat model (high level)

QFA processes potentially sensitive free-text feedback and sends prompts to an external LLM provider.  
Main risks are:

1. exposure of sensitive beneficiary data (PII) in prompts/logs,
2. unauthorized API use,
3. prompt-injection/manipulation through feedback text,
4. deployment misconfiguration (networking, IAM, secrets, CI/CD).

## Current security posture (what QFA secures by default)

QFA currently provides these baseline controls:

- API-key authentication for all analysis endpoints (`Authorization: Bearer <key>`).
- Constant-time key comparison (`secrets.compare_digest`).
- Structured error envelopes with request IDs.
- Basic prompt-injection pattern checks in the orchestrator.
- PII anonymization/deanonymization in orchestrator flows (enabled by default where configurable).
- Azure-oriented deployment baseline with HTTPS-only App Service, managed identities, and Key Vault references for secrets.
- No application database by default (reduced at-rest data footprint in app layer).

## What QFA does **not** secure by default

QFA is **not** a full security boundary on its own. In particular:

- It does not enforce network isolation by itself (private ingress/egress must be configured in deployment).
- It does not provide tenant isolation beyond API-key level.
- It does not provide DLP/compliance guarantees for all LLM-provider behaviors by itself.
- It does not include full SIEM/SOC monitoring, IDS/IPS, or incident response automation.
- It does not automatically rotate/revoke API keys without operational procedures.

## Main risks identified (high level review)

### High priority

1. **Anonymization can be disabled by API callers (for some endpoints).**  
   This increases risk of transmitting raw PII to the LLM in sensitive contexts.
2. **Prompt-injection controls are heuristic and limited.**  
   Current regex checks are helpful but not sufficient against broader instruction/data attacks.
3. **Key Vault purge protection is disabled in Terraform.**  
   This weakens recovery posture for accidental/malicious secret deletion.

### Medium priority

4. **Potential sensitive details in propagated error messages/logs.**  
   Provider exception strings should be treated as potentially sensitive and sanitized.
5. **Documented LLM retention guard (`store=False`) is not explicitly set in the LLM call path.**
6. **CI/CD identity has broad permissions (Contributor at RG scope).**  
   Acceptable for simplicity, but should be narrowed over time.

### Lower priority

7. **Container runs as root by default.**
8. **Some security-related docs and implementation details are slightly out of sync.**

## Security principles and minimum deployment expectations

For all deployments (especially humanitarian/sensitive use), minimum expectations are:

1. **Data minimization**  
   Share only required fields with QFA and only required fields from QFA to LLM.
2. **Least privilege**  
   Limit IAM roles for app runtime, CI/CD, and operators; separate duties where practical.
3. **Defense in depth**  
   Combine app controls with network controls, WAF/API gateway controls, and platform controls.
4. **Secure secrets lifecycle**  
   Use managed secret stores, key rotation runbooks, revocation procedures, and access auditing.
5. **Safe observability**  
   Log metadata, never payload content or secrets; centralize logs and alerts.
6. **Operational readiness**  
   Define incident response, backup/recovery, and patch/update procedures.
7. **Context-appropriate compliance**  
   Align deployment with GDPR and ICRC data-protection principles (lawful basis, minimization, retention, access governance).

## Recommended improvements (prioritized)

1. **Enforce anonymization policy**
   - Keep anonymization always-on by default; allow bypass only via explicit operator-controlled policy.
2. **Strengthen prompt/data boundary**
   - Harden injection defenses and safely escape/structure user-supplied text and metadata.
3. **Sanitize error handling and logging**
   - Avoid returning raw provider error strings to clients; reduce sensitive detail in logs.
4. **Harden Key Vault configuration**
   - Enable purge protection (at least staging/prd) and review network access restrictions.
5. **Enforce LLM data-retention intent in code**
   - Explicitly set provider options equivalent to `store=False` where supported.
6. **Reduce CI/CD blast radius**
   - Split Terraform and deployment identities/permissions where feasible.
7. **Run container as non-root**
   - Drop root privileges in the runtime image.

## Developer and deployer hardening checklist

- [ ] Use HTTPS end-to-end and trusted TLS termination.
- [ ] Restrict inbound access (IP allow-list, private endpoints, or gateway/WAF controls).
- [ ] Restrict outbound access to required LLM endpoints only.
- [ ] Store all secrets in Key Vault; never in code or CI logs.
- [ ] Rotate API keys periodically and on personnel/incident triggers.
- [ ] Enable centralized logging/monitoring and alerting for auth failures and anomalies.
- [ ] Verify no payload text or API keys are logged.
- [ ] Review LLM-provider privacy/retention settings for the chosen provider/region.
- [ ] Document and test incident response and recovery procedures.

## Follow-up implementation issue (draft)

**Title:** Implement QFA baseline hardening actions (phase 1)

**Proposed scope**

- [ ] Make anonymization bypass operator-controlled (or remove public bypass).
- [ ] Add explicit LLM retention control (`store=False` equivalent) and tests.
- [ ] Sanitize provider errors returned to API clients.
- [ ] Enable Key Vault purge protection for staging/prd Terraform configuration.
- [ ] Add/adjust tests for the above security behaviors.

This draft issue is intentionally implementation-focused and should be tracked separately from this assessment.
