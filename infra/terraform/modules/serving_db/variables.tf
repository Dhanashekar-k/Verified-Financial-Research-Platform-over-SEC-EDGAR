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
  description = "Non-burstable on purpose: flex and t-class instances throttle under the sustained load of an HNSW build and a latency bench (ADR-034)."
  type        = string
  default     = "m7i.large"
}

variable "volume_gb" {
  description = "Root volume (gp3): Postgres data, export staging and Docker images."
  type        = number
  default     = 60
}
