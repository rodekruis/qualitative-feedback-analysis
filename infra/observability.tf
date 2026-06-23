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

# =============================================================================
# Alerting
# =============================================================================

# Defines where alerts are sent. Reused by all alert rules below so the
# email address only needs to be changed in one place.
resource "azurerm_monitor_action_group" "email" {
  name                = "qfa-${local.env}-alerts"
  resource_group_name = data.azurerm_resource_group.main.name
  short_name          = "qfa-alerts"

  email_receiver {
    name          = "olaf"
    email_address = "olaf@aurai.com"
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
    action_group_id = azurerm_monitor_action_group.email.id
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
    aggregation      = "Average"
    operator         = "LessThan"
    threshold        = 1
  }

  action {
    action_group_id = azurerm_monitor_action_group.email.id
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
    action_group_id = azurerm_monitor_action_group.email.id
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
    action_group_id = azurerm_monitor_action_group.email.id
  }
}
