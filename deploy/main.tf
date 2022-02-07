provider "azurerm" {
  features {}
}

locals {
  common_name = join("-", [var.short_location, var.product, var.environment, "app-insights"])

  tags = {
    environment = var.environment
    product     = var.product
  }
}

data "azurerm_client_config" "terraform" {}

resource "azurerm_resource_group" "main" {
  location = var.location
  name     = local.common_name
  tags     = local.tags
}

resource "azurerm_log_analytics_workspace" "main" {
  location            = azurerm_resource_group.main.location
  name                = local.common_name
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "Free"
  tags                = merge(local.tags, {x = 7})

#  table {
#    name              = "AppEvents"
#    retention_in_days = 7
#  }
#
#  table {
#    name              = "AppMetrics"
#    retention_in_days = 7
#  }
}

#data "azurerm_log_analytics_workspace" "main" {
#  name                = azurerm_log_analytics_workspace.main.name
#  resource_group_name = azurerm_log_analytics_workspace.main.resource_group_name
#}

#output "azurerm_log_analytics_workspace_tables" {
#  value = azurerm_log_analytics_workspace.main.table
#}

resource "azurerm_application_insights" "main" {
  application_type    = "other"
  location            = azurerm_resource_group.main.location
  name                = local.common_name
  resource_group_name = azurerm_resource_group.main.name
  retention_in_days   = 30
  tags                = local.tags
  workspace_id        = azurerm_log_analytics_workspace.main.id
}

resource "azuread_application" "app_insights_reader" {
  display_name = join("-", [local.common_name, "app-insights-reader"])
}

resource "azuread_service_principal" "app_insights_reader" {
  application_id = azuread_application.app_insights_reader.application_id
}

resource "azuread_service_principal_password" "app_insights_reader" {
  service_principal_id = azuread_service_principal.app_insights_reader.id
}

resource "azurerm_role_assignment" "app_insights_reader" {
  principal_id = azuread_service_principal.app_insights_reader.id
  scope        = azurerm_application_insights.main.id

  role_definition_name = "Monitoring Reader"
}

resource "azurerm_role_assignment" "log_analytics_reader" {
  principal_id = azuread_service_principal.app_insights_reader.id
  scope        = azurerm_log_analytics_workspace.main.id

  role_definition_name = "Monitoring Reader"
}

resource "local_file" "test_env" {
  filename = join("/", [path.root, "generated", "test-env.json"])

  content = jsonencode({
    APP_INSIGHTS_APP_ID              = base64encode(azurerm_application_insights.main.app_id)
    APP_INSIGHTS_INSTRUMENTATION_KEY = azurerm_application_insights.main.instrumentation_key
    AZURE_SP_ID                      = base64encode(azuread_application.app_insights_reader.application_id)
    AZURE_SP_KEY                     = base64encode(azuread_service_principal_password.app_insights_reader.value)
    AZURE_SP_TENANT                  = base64encode(data.azurerm_client_config.terraform.tenant_id)
    LOG_ANALYTICS_WORKSPACE_ID       = base64encode(azurerm_log_analytics_workspace.main.workspace_id)
  })
}
