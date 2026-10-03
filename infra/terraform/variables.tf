variable "subscription_id" {
  description = "Target subscription ID (terraform.tfvars, git-ignored)."
  type        = string
}

variable "location" {
  description = "Azure region."
  type        = string
  default     = "eastus"
}

variable "prefix" {
  description = "Short lowercase name prefix."
  type        = string
  default     = "collab"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{2,9}$", var.prefix))
    error_message = "prefix: 3-10 lowercase letters/digits, starting with a letter."
  }
}

variable "environment" {
  description = "Environment name used in resource names."
  type        = string
  default     = "prod"
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default = {
    project    = "ai-collab-hub"
    managed-by = "terraform"
  }
}

# --- identity / auth ---------------------------------------------------------

variable "api_client_id" {
  description = "Client ID of the Entra API app (bootstrap output api_client_id); the token audience."
  type        = string
}

variable "collab_principals" {
  description = "Allowed identities: Entra object ID => grant. Keep in terraform.tfvars (git-ignored)."
  type = map(object({
    alias     = string
    workspace = optional(string, "main")
    role      = optional(string, "writer")
  }))

  validation {
    condition     = length(var.collab_principals) > 0
    error_message = "At least one principal is required; the server has no anonymous mode."
  }

  validation {
    condition     = alltrue([for p in values(var.collab_principals) : contains(["reader", "writer"], p.role)])
    error_message = "role must be reader or writer."
  }
}

variable "enable_oauth" {
  description = "Run the hub's OAuth authorization server for ChatGPT / Claude.ai connectors (Entra sign-in, no secrets)."
  type        = bool
  default     = true
}

variable "deploy_principal_object_id" {
  description = "Object ID of the GitHub OIDC deploy service principal (bootstrap output). Null = no CI deploy roles."
  type        = string
  default     = null
}

# --- app ---------------------------------------------------------------------

variable "deploy_app" {
  description = "Create the Container App. First apply with false, push an image, then true (see docs/runbook.md)."
  type        = bool
  default     = false
}

variable "container_image" {
  description = "Initial image, e.g. <acr>.azurecr.io/collab-hub:<git-sha>. Later deploys update it out of band."
  type        = string
  default     = ""

  validation {
    condition     = var.container_image == "" || can(regex("^[a-z0-9.-]+\\.azurecr\\.io/[a-z0-9._/-]+:[A-Za-z0-9._-]+$", var.container_image))
    error_message = "container_image must be <registry>.azurecr.io/<repo>:<tag>."
  }
}

variable "app_cpu" {
  description = "vCPU per replica (consumption: 0.25 steps)."
  type        = number
  default     = 0.25
}

variable "app_memory" {
  description = "Memory per replica; must match the CPU ratio (0.25 vCPU => 0.5Gi)."
  type        = string
  default     = "0.5Gi"
}

variable "min_replicas" {
  description = "0 = scale to zero when idle (cold start of a few seconds on the first call)."
  type        = number
  default     = 0
}

variable "max_replicas" {
  description = "Upper bound on replicas (the server is stateless)."
  type        = number
  default     = 2
}

# --- storage / logs ------------------------------------------------------------

variable "storage_replication" {
  description = "Replication for the data storage account (LRS or ZRS for higher durability)."
  type        = string
  default     = "LRS"

  validation {
    condition     = contains(["LRS", "ZRS", "GRS", "GZRS"], var.storage_replication)
    error_message = "storage_replication must be LRS, ZRS, GRS or GZRS."
  }
}

variable "blob_soft_delete_days" {
  description = "Soft delete retention for blobs and containers."
  type        = number
  default     = 14
}

variable "noncurrent_version_days" {
  description = "Delete non-current blob versions after this many days (lifecycle policy)."
  type        = number
  default     = 90
}

variable "protect_data" {
  description = "Put a CanNotDelete lock on the data storage account. Remove it deliberately before teardown."
  type        = bool
  default     = true
}

variable "acr_sku" {
  description = "Container registry SKU."
  type        = string
  default     = "Basic"
}

variable "log_retention_days" {
  description = "Log Analytics retention (30 is included in the ingestion price)."
  type        = number
  default     = 30
}

variable "log_daily_quota_gb" {
  description = "Log Analytics daily ingestion cap in GB (-1 = unlimited). Caps runaway log cost."
  type        = number
  default     = 0.2
}

variable "enable_storage_diagnostics" {
  description = "Send storage write/delete logs and transaction metrics to Log Analytics."
  type        = bool
  default     = true
}
