## Summary

The Azure Monitor infrastructure shipped in #207 is validated (logs, metrics, alert rules), and the OpenTelemetry SDK is wired in `src/qfa/main.py`. But in the `qfa-dev-appinsights` resource the request charts **flatline at zero even when requests are fired at the backend** — the App Insights `App*` tables (`AppRequests`, `AppDependencies`, `AppExceptions`, `AppTraces`) are not populating. Deferred out of #207 (which ships log shipping + metrics + alert triggers); this issue tracks getting application telemetry to actually flow.

## The one hard fact

Sending requests to the `dev` backend produces **no `AppRequests`** in `qfa-dev-appinsights` (charts stay at zero, past ingestion lag). Everything else in #207 is confirmed working.

## Hypotheses — NOT yet validated

> The only confirmed evidence is the flatline. The items below are candidates to investigate, not a diagnosis.

1. **Deployed image predates the OTel wiring.** `configure_azure_monitor()` init landed in `main.py` late on the branch (after `cfdbb86`, "source connection string from settings"). If the running `dev` container was built/deployed before that, it has no exporter.
2. **App crash-looping → batched exporter never flushes.** `dev` showed OOM/`SIGKILL` and ~107 s cold starts near the 230 s `ContainerTimeout` on the B2 plan. OTel batches telemetry; a container killed mid-startup loses it before send.
3. **Connection string not resolving in the container.** SDK init is gated on `APPLICATIONINSIGHTS_CONNECTION_STRING`; if the app setting is empty/malformed at runtime, init is silently skipped.
4. **Exporter failing silently** — egress to the ingestion endpoint blocked, or wrong regional endpoint.

## Diagnostic order

1. **Live Metrics** (`qfa-dev-appinsights` → Live Metrics) while curling `/v1/health`: *connected + ticks* → emitting (then it's ingestion/query/time-range); *not connected* → not emitting (hypotheses 1–4).
2. Confirm the running `dev` image is built from a commit that includes `main.py`'s OTel init.
3. Verify `APPLICATIONINSIGHTS_CONNECTION_STRING` is set and non-empty in the running app, and that the app isn't crash-looping (`AppServicePlatformLogs` for `ContainerTimeout`/OOM).
4. Check `AppServiceConsoleLogs` for azure-monitor exporter errors at startup.

## Acceptance criteria

- [ ] Root cause of the empty `App*` tables identified from the steps above.
- [ ] Firing requests produces `AppRequests` rows and Live Metrics shows "connected".
- [ ] `AppDependencies` shows Postgres/OpenAI calls (Application Map renders).
- [ ] Remove the "Verification pending" note in `docs/operations/observability.md` once confirmed.

## References

- Follow-up to #207; sibling of #222 (Teams delivery).
- `src/qfa/main.py` (OTel init), `infra/app_service.tf` (connection-string wiring), `infra/observability.tf` (App Insights resource).
