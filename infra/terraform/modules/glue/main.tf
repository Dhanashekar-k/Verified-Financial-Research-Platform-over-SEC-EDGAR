# The Spark jobs run on AWS Glue because an Organization policy denies EMR Serverless (ADR-027).
# Same PySpark as the laptop and EMR: the scripts in s3://<lake>/scripts/ are thin entry points
# (pipelines/glue/*_job.py) over the shared jobs in pipelines/spark/, shipped as pipelines.zip.
# Creating a job costs nothing; only a run is billed (DPU-seconds, 1-minute minimum).

locals {
  lake_bucket_arn = "arn:aws:s3:::${var.lake_bucket_name}"
  glue_arn        = "arn:aws:glue:${var.region}:${var.account_id}"

  # raw/curated plus the smoke databases, so a smoke run can never touch the real tables.
  databases = concat(var.glue_database_names, [for db in aws_glue_catalog_database.smoke : db.name])

  scripts_uri = "s3://${var.lake_bucket_name}/scripts"
  code_uri    = "s3://${var.lake_bucket_name}/artifacts/glue/pipelines.zip"

  # Glue reads several --conf settings from ONE argument: "a=b --conf c=d". These must equal
  # pipelines/common/spark_session.py build_spark_conf("glue"); that function also sets them when
  # the session is built, so a drift here would show up as a failed run, not a silent difference.
  iceberg_conf = join(" --conf ", [
    "spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
    "spark.sql.catalog.lake=org.apache.iceberg.spark.SparkCatalog",
    "spark.sql.catalog.lake.catalog-impl=org.apache.iceberg.aws.glue.GlueCatalog",
    "spark.sql.catalog.lake.io-impl=org.apache.iceberg.aws.s3.S3FileIO",
    "spark.sql.catalog.lake.warehouse=s3://${var.lake_bucket_name}/warehouse",
    "spark.sql.session.timeZone=UTC",
    "spark.sql.csv.parser.columnPruning.enabled=false",
  ])

  # Python libraries Glue installs from PyPI on the driver and every executor before the script
  # runs. Glue 5.0 has none of them. The versions equal infra/emr/requirements-emr.txt (which
  # equals uv.lock), so Glue parses with the same code the tests ran against.
  parsing_modules = "selectolax==1.0.2,tokenizers==0.23.3,pymupdf==2.1,pydantic==2.14.3"

  jobs = {
    "edgarqa-fsds-to-iceberg"   = { script = "fsds_to_iceberg_job.py", extra_args = {} }
    "edgarqa-normalize-facts"   = { script = "normalize_facts_job.py", extra_args = {} }
    "edgarqa-load-filings-html" = { script = "load_filings_html_job.py", extra_args = {} }
    "edgarqa-parse-filings" = {
      script     = "parse_filings_job.py"
      extra_args = { "--additional-python-modules" = local.parsing_modules }
    }
  }
}

# Smoke tests write here (--namespace lake.raw_smoke / lake.curated_smoke), never to raw/curated.
# Off by default: set smoke_databases = ["raw_smoke", "curated_smoke"] for a smoke run, then
# remove them again (dropping a database drops its tables; the S3 files are deleted separately).
resource "aws_glue_catalog_database" "smoke" {
  for_each    = toset(var.smoke_databases)
  name        = each.value
  description = "Throwaway tables from Glue smoke runs; safe to drop."
}

resource "aws_glue_job" "this" {
  for_each = local.jobs

  name              = each.key
  role_arn          = aws_iam_role.job.arn
  glue_version      = var.glue_version
  worker_type       = var.worker_type
  number_of_workers = var.number_of_workers
  timeout           = var.timeout_minutes # minutes; a stuck job stops billing at this limit
  max_retries       = 0                   # a retry would bill the failed work twice
  execution_class   = "STANDARD"

  command {
    name            = "glueetl"
    script_location = "${local.scripts_uri}/${each.value.script}"
    python_version  = "3"
  }

  # Per-run arguments (company, year, limit, quarters) are passed to StartJobRun, e.g.
  #   --ciks 320193 --quarters 2023q3,2023q4 --namespace lake.raw_smoke
  # They are not defaults on purpose: a forgotten default would silently shrink a full run.
  default_arguments = merge(each.value.extra_args, {
    "--datalake-formats"                 = "iceberg"
    "--conf"                             = local.iceberg_conf
    "--extra-py-files"                   = local.code_uri
    "--lake-bucket"                      = var.lake_bucket_name
    "--job-language"                     = "python"
    "--TempDir"                          = "s3://${var.lake_bucket_name}/tmp/glue/"
    "--enable-metrics"                   = "true"
    "--enable-continuous-cloudwatch-log" = "true"
    "--enable-job-insights"              = "false"
  })

  depends_on = [aws_cloudwatch_log_group.glue]
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
      [for db in local.databases : "${local.glue_arn}:database/${db}"],
      [for db in local.databases : "${local.glue_arn}:table/${db}/*"],
    )
  }

  # `--enable-metrics` publishes job metrics; PutMetricData has no resource-level scoping.
  statement {
    sid       = "JobMetrics"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["Glue"]
    }
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
