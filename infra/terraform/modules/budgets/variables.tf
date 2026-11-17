variable "alert_email" {
  description = "Email address that receives budget alerts."
  type        = string
}

variable "monthly_limit_usd" {
  description = "Monthly budget limit in USD. Alerts fire at $20, $50 and $80 of actual spend."
  type        = number
  default     = 80
}
