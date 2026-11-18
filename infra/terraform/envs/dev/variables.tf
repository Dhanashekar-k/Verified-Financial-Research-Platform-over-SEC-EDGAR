variable "account_id" {
  description = "AWS account ID this environment may touch (guards against a wrong CLI profile)."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be a 12-digit AWS account ID."
  }
}

variable "region" {
  description = "AWS region."
  type        = string
  default     = "us-east-1"
}

variable "alert_email" {
  description = "Email address that receives budget alerts."
  type        = string
}

variable "github_repo" {
  description = "GitHub repository allowed to assume the OIDC role, as owner/name."
  type        = string

  validation {
    condition     = can(regex("^[^/]+/[^/]+$", var.github_repo))
    error_message = "github_repo must look like owner/name."
  }
}

variable "emr_image_uri" {
  description = "Custom EMR image URI with tag (<ecr repository_url>:<git-sha>); empty until the image is pushed."
  type        = string
  default     = ""
}
