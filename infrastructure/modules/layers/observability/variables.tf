variable "environment" {
  description = "Deployment environment (dev or prod)"
  type        = string
}

variable "zip_path" {
  description = "Absolute or workspace-relative path to the knotify-observability-layer.zip produced by build.sh"
  type        = string
}
