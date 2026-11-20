# EMR Serverless Spark application for the lakehouse jobs (M1 normalize_facts, M2 parse_filings, M3).
# Arm64, stops itself when idle, and cannot grow past max_*: those three settings are the cost guard.

locals {
  lake_bucket_arn = "arn:aws:s3:::${var.lake_bucket_name}"
  glue_arn        = "arn:aws:glue:${var.region}:${var.account_id}"
}

resource "aws_emrserverless_application" "spark" {
  count         = var.create_application ? 1 : 0
  name          = "edgarqa-spark"
  release_label = var.release_label
  type          = "spark"
  architecture  = "ARM64"

  auto_start_configuration {
    enabled = true
  }

  auto_stop_configuration {
    enabled              = true
    idle_timeout_minutes = var.idle_timeout_minutes
  }

  maximum_capacity {
    cpu    = "${var.max_vcpu} vCPU"
    memory = "${var.max_memory_gb} GB"
    disk   = "${var.max_disk_gb} GB"
  }

  dynamic "image_configuration" {
    for_each = var.image_uri == "" ? [] : [var.image_uri]
    content {
      image_uri = image_configuration.value
    }
  }
}

data "aws_iam_policy_document" "job_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["emr-serverless.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "job" {
  name               = "edgarqa-emr-job"
  assume_role_policy = data.aws_iam_policy_document.job_trust.json
}

data "aws_iam_policy_document" "job" {
  # The jobs read raw files and write Iceberg tables, job logs and metrics under the lake bucket.
  # AbortMultipartUpload cleans up parts when a task fails or is speculated mid-upload.
  statement {
    sid     = "LakeObjects"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
    resources = [
      "${local.lake_bucket_arn}/*",
    ]
  }

  # Bucket-level calls Iceberg and the S3 client need: listing, location, and finding open uploads.
  statement {
    sid       = "LakeBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads"]
    resources = [local.lake_bucket_arn]
  }

  # The Iceberg Glue catalog creates, reads and updates tables in the raw and curated databases only.
  statement {
    sid    = "GlueCatalog"
    effect = "Allow"
    actions = [
      "glue:GetDatabase",
      "glue:GetDatabases",
      "glue:GetTable",
      "glue:GetTables",
      "glue:CreateTable",
      "glue:UpdateTable",
      "glue:DeleteTable",
      "glue:GetPartitions",
      "glue:BatchGetPartition",
    ]
    resources = concat(
      ["${local.glue_arn}:catalog"],
      [for db in var.glue_database_names : "${local.glue_arn}:database/${db}"],
      [for db in var.glue_database_names : "${local.glue_arn}:table/${db}/*"],
    )
  }
}

resource "aws_iam_role_policy" "job" {
  name   = "edgarqa-emr-job"
  role   = aws_iam_role.job.id
  policy = data.aws_iam_policy_document.job.json
}
