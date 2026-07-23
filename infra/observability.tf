# =============================================================================
# Observability
# =============================================================================

# Central storage for all logs and metrics. Every other observability resource
# sends data here. Queryable via KQL in the Azure Portal.
resource "azurerm_log_analytics_workspace" "main" {
  name                = "qfa-${local.env}-logs"
  resource_group_name = data.azurerm_resource_group.main.name
  location            = data.azurerm_resource_group.main.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
}

# Monitoring dashboard on top of the Log Analytics workspace. Provides
# pre-built views for request rates, response times, and failures.
# The app connects to this via APPLICATIONINSIGHTS_CONNECTION_STRING in app_settings.
resource "azurerm_application_insights" "main" {
  name                = "qfa-${local.env}-appinsights"
  resource_group_name = data.azurerm_resource_group.main.name
  location            = data.azurerm_resource_group.main.location
  workspace_id        = azurerm_log_analytics_workspace.main.id
  application_type    = "web"
}

# Routes App Service stdout/stderr and HTTP access logs into the Log Analytics
# workspace above, making them searchable via KQL in the Azure Portal.
# Without this, logs only exist in the live App Service log stream and cannot
# be queried after the fact or used to trigger log-based alerts.
#
# Useful KQL queries once data arrives (Azure Portal → Log Analytics workspace
# → Logs):
#   AppServiceConsoleLogs                           -- all stdout/stderr
#   | where ResultDescription contains "ERROR"
#
#   AppServiceHTTPLogs                              -- per-request HTTP log
#   | where ScStatus >= 500
#   | summarize count() by bin(TimeGenerated, 1h)
resource "azurerm_monitor_diagnostic_setting" "app_service" {
  name                       = "qfa-${local.env}-backend-diag"
  target_resource_id         = azurerm_linux_web_app.backend.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id

  enabled_log {
    category = "AppServiceConsoleLogs" # stdout/stderr — where Python logging output goes
  }

  enabled_log {
    category = "AppServiceHTTPLogs" # HTTP access log: status, latency, path per request
  }

  enabled_log {
    category = "AppServicePlatformLogs" # container restarts, scaling events
  }

  metric {
    category = "AllMetrics"
  }
}

# =============================================================================
# Alerting
# =============================================================================

# Defines where alerts are sent. Reused by all alert rules below so the
# webhook only needs to be changed in one place.
#
# The webhook URL comes from var.teams_webhook_url (TF_VAR_teams_webhook_url —
# local shell/.env, or a GitHub Actions secret in CI), not Key Vault: Action
# Group webhook receivers have no equivalent of App Service's
# `@Microsoft.KeyVault(...)` app_settings resolver (app_service.tf), so a
# Key-Vault-backed value would need a `data` source and land in Terraform
# state either way (plus its own RBAC grant for whoever runs plan/apply).
# Passing it straight in as a variable gets the same state exposure without
# that extra Key Vault dependency — acceptable here since a leaked webhook
# only allows posting to the Teams channel, unlike llm-api-key/auth-api-keys.
#
# use_common_alert_schema = true standardizes the POSTed payload across alert
# types, but Azure still sends raw JSON — Teams will render it as an
# unformatted text block, not a styled card. Acceptable for now to get
# alerting working; revisit with a Power Automate parse step or a Logic App
# intermediary if the raw payload proves too noisy in practice.
resource "azurerm_monitor_action_group" "alerts" {
  name                = "qfa-${local.env}-alerts"
  resource_group_name = data.azurerm_resource_group.main.name
  short_name          = "qfa-alerts"

  webhook_receiver {
    name                    = "teams"
    service_uri             = var.teams_webhook_url
    use_common_alert_schema = true
  }
}

# Fires when the app returns more than 5 HTTP 500-level errors in 5 minutes,
# indicating the app is crashing or failing to handle requests.
resource "azurerm_monitor_metric_alert" "http_5xx" {
  name                = "qfa-${local.env}-http-5xx"
  resource_group_name = data.azurerm_resource_group.main.name
  scopes              = [azurerm_linux_web_app.backend.id]
  description         = "Alert when HTTP 5xx error rate exceeds 5 in 5 minutes"
  severity            = 2
  frequency           = "PT5M"
  window_size         = "PT5M"

  criteria {
    metric_namespace = "Microsoft.Web/sites"
    metric_name      = "Http5xx"
    aggregation      = "Total"
    operator         = "GreaterThan"
    threshold        = 5
  }

  action {
    action_group_id = azurerm_monitor_action_group.alerts.id
  }
}

# Fires when the /v1/health endpoint fails, indicating the app is down.
# HealthCheckStatus drops to 0 when the health check fails — this is the most
# direct signal that the container is unhealthy or failed to start.
resource "azurerm_monitor_metric_alert" "health_check" {
  name                = "qfa-${local.env}-health-check"
  resource_group_name = data.azurerm_resource_group.main.name
  scopes              = [azurerm_linux_web_app.backend.id]
  description         = "Alert when the /v1/health endpoint fails"
  severity            = 1
  frequency           = "PT5M"
  window_size         = "PT5M"

  criteria {
    metric_namespace = "Microsoft.Web/sites"
    metric_name      = "HealthCheckStatus"
    aggregation      = "Count"
    operator         = "LessThan"
    threshold        = 4
  }

  action {
    action_group_id = azurerm_monitor_action_group.alerts.id
  }
}

# Fires when CPU on the App Service Plan exceeds 80% for 5 minutes.
# On a B2 (2 vCPU), the embedding model loading spikes CPU at startup.
resource "azurerm_monitor_metric_alert" "high_cpu" {
  name                = "qfa-${local.env}-high-cpu"
  resource_group_name = data.azurerm_resource_group.main.name
  scopes              = [azurerm_service_plan.main.id]
  description         = "Alert when CPU usage exceeds 80% for 5 minutes"
  severity            = 2
  frequency           = "PT5M"
  window_size         = "PT5M"

  criteria {
    metric_namespace = "Microsoft.Web/serverFarms"
    metric_name      = "CpuPercentage"
    aggregation      = "Average"
    operator         = "GreaterThan"
    threshold        = 80
  }

  action {
    action_group_id = azurerm_monitor_action_group.alerts.id
  }
}

# Fires when memory on the App Service Plan exceeds 80% for 5 minutes.
# The embedding model (~150MB) sits in RAM after loading — monitor for leaks
# or repeated model reloads driving memory up over time.
resource "azurerm_monitor_metric_alert" "high_memory" {
  name                = "qfa-${local.env}-high-memory"
  resource_group_name = data.azurerm_resource_group.main.name
  scopes              = [azurerm_service_plan.main.id]
  description         = "Alert when memory usage exceeds 80% for 5 minutes"
  severity            = 2
  frequency           = "PT5M"
  window_size         = "PT5M"

  criteria {
    metric_namespace = "Microsoft.Web/serverFarms"
    metric_name      = "MemoryPercentage"
    aggregation      = "Average"
    operator         = "GreaterThan"
    threshold        = 80
  }

  action {
    action_group_id = azurerm_monitor_action_group.alerts.id
  }
}
