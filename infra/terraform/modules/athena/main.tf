# Ad-hoc SQL over the lake (about $5 per TB scanned). The cutoff cancels a runaway query.
resource "aws_athena_workgroup" "edgarqa" {
  name = "edgarqa"

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = false
    bytes_scanned_cutoff_per_query     = var.bytes_scanned_cutoff_per_query

    result_configuration {
      output_location = "s3://${var.lake_bucket_name}/athena-results/"

      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }
  }
}
