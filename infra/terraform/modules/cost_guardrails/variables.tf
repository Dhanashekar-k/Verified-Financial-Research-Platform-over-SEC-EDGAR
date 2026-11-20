variable "alert_email" {
  description = "Email address that receives the daily Cost Anomaly Detection summary."
  type        = string
}

variable "cost_allocation_tag_key" {
  description = "Tag key activated as a cost allocation tag so Cost Explorer can group by it."
  type        = string
  default     = "project"
}

variable "activate_cost_allocation_tag" {
  description = "Activate the cost allocation tag. Only works in an Organization's management account (or a standalone account)."
  type        = bool
  default     = false
}
