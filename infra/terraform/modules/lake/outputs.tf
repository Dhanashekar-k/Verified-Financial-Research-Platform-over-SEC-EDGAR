output "bucket_name" {
  description = "Name of the lake S3 bucket."
  value       = aws_s3_bucket.lake.bucket
}

output "bucket_arn" {
  description = "ARN of the lake S3 bucket."
  value       = aws_s3_bucket.lake.arn
}

output "glue_database_names" {
  description = "Glue database names by layer."
  value = {
    raw     = aws_glue_catalog_database.raw.name
    curated = aws_glue_catalog_database.curated.name
  }
}
