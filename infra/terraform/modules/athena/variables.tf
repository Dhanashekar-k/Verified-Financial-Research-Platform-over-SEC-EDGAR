variable "lake_bucket_name" {
  description = "Lake bucket; query results go under athena-results/ (expired after 7 days by the lake module)."
  type        = string
}

variable "bytes_scanned_cutoff_per_query" {
  description = "A query that would scan more than this is cancelled (04_aws_and_cost_plan.md §2 item 8). Default 10 GB."
  type        = number
  default     = 10737418240
}
