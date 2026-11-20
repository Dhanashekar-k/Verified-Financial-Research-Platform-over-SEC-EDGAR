# Execution role and log groups for the Spark jobs that run on AWS Glue (the Free plan has no EMR Serverless).
# No Glue job is created here: a role and log groups cost nothing, jobs are added in the next phase.

locals {
  lake_bucket_arn = "arn:aws:s3:::${var.lake_bucket_name}"
  glue_arn        = "arn:aws:glue:${var.region}:${var.account_id}"
}

data "aws_iam_policy_document" "trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["glue.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "job" {
  name               = "edgarqa-glue-job"
  assume_role_policy = data.aws_iam_policy_document.trust.json
}

data "aws_iam_policy_document" "job" {
  # Same data access as the EMR job role: raw files in, Iceberg tables and job output out, lake bucket only.
  statement {
    sid       = "LakeObjects"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
    resources = ["${local.lake_bucket_arn}/*"]
  }

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

  # Glue writes driver and executor logs to these log groups only.
  statement {
    sid       = "JobLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = [for g in aws_cloudwatch_log_group.glue : "${g.arn}:*"]
  }
}

resource "aws_iam_role_policy" "job" {
  name   = "edgarqa-glue-job"
  role   = aws_iam_role.job.id
  policy = data.aws_iam_policy_document.job.json
}

# Glue's default log groups, created here so they carry 7-day retention instead of "never expire".
resource "aws_cloudwatch_log_group" "glue" {
  for_each          = toset(["/aws-glue/jobs/logs-v2", "/aws-glue/jobs/output", "/aws-glue/jobs/error"])
  name              = each.value
  retention_in_days = var.log_retention_days
}
