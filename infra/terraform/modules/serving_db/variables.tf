variable "enabled" {
  description = "Create the serving box. Off by default: the box costs money while it exists."
  type        = bool
  default     = false
}

variable "lake_bucket_name" {
  description = "The lake bucket the box reads exports and code from and writes bench results to."
  type        = string
}

variable "instance_type" {
  description = "m7i-flex.large: the largest Free-plan-eligible type (2 vCPU, 8 GiB). The Free plan refused m7i.large on 2026-11-22 (ADR-034); flex has a CPU baseline, so build and latency numbers from it are preliminary."
  type        = string
  default     = "m7i-flex.large"
}

variable "volume_gb" {
  description = "Root volume (gp3): Postgres data, export staging and Docker images."
  type        = number
  default     = 60
}
