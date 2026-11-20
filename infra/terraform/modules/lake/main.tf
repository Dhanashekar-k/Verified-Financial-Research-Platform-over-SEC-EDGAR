resource "aws_s3_bucket" "lake" {
  bucket = "edgarqa-lake-${var.account_id}"
}

# Versioning is intentionally not configured (off), per M00 §3.6.

resource "aws_s3_bucket_public_access_block" "lake" {
  bucket                  = aws_s3_bucket.lake.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # Athena query results (04_aws_and_cost_plan.md §2 item 8).
  rule {
    id     = "expire-athena-results"
    status = "Enabled"

    filter {
      prefix = "athena-results/"
    }

    expiration {
      days = 7
    }
  }
}

resource "aws_glue_catalog_database" "raw" {
  name        = "raw"
  description = "Raw SEC filings and XBRL facts as ingested."
}

resource "aws_glue_catalog_database" "curated" {
  name        = "curated"
  description = "Curated Iceberg tables used for serving."
}
