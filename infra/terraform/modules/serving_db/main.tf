# The M3 serving box (ADR-034): one x86_64 EC2 instance running Postgres 17 + pgvector in Docker.
# Loader and bench run ON the box through SSM, so no inbound port is ever opened and the HNSW
# build is not tied to anyone's laptop session. Everything here is gated by var.enabled.

locals {
  count_if_enabled = var.enabled ? 1 : 0
  bucket_arn       = "arn:aws:s3:::${var.lake_bucket_name}"
}

data "aws_ssm_parameter" "al2023" {
  count = local.count_if_enabled
  name  = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

data "aws_vpc" "default" {
  count   = local.count_if_enabled
  default = true
}

data "aws_subnets" "default" {
  count = local.count_if_enabled
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default[0].id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

# No ingress rule at all: SSM Session Manager and the SSM agent only need outbound HTTPS.
resource "aws_security_group" "box" {
  count       = local.count_if_enabled
  name        = "edgarqa-serving-box"
  description = "Serving box: no inbound, outbound only"
  vpc_id      = data.aws_vpc.default[0].id

  # HTTPS only: the SSM agent, S3, the dnf repositories, Docker Hub, PyPI and GitHub (uv's Python
  # download) all use 443. DNS goes to the VPC resolver, which security groups do not filter.
  egress {
    description = "Outbound HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

data "aws_iam_policy_document" "trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "box" {
  count              = local.count_if_enabled
  name               = "edgarqa-serving-box"
  assume_role_policy = data.aws_iam_policy_document.trust.json
}

# Lets the SSM agent register the instance and run commands (managed policy, no data access).
resource "aws_iam_role_policy_attachment" "ssm" {
  count      = local.count_if_enabled
  role       = aws_iam_role.box[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "data" {
  # Read the export, the code bundle and the embedding model; nothing else in the lake.
  statement {
    sid     = "ReadExportsCodeModel"
    effect  = "Allow"
    actions = ["s3:GetObject"]
    resources = [
      "${local.bucket_arn}/exports/*",
      "${local.bucket_arn}/artifacts/serving/*",
      "${local.bucket_arn}/artifacts/models/*",
    ]
  }

  statement {
    sid       = "ListThoseKeys"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [local.bucket_arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["exports/*", "artifacts/serving/*", "artifacts/models/*"]
    }
  }

  # Needed by S3 clients to resolve the bucket's region; the action has no prefix condition key.
  statement {
    sid       = "BucketLocation"
    effect    = "Allow"
    actions   = ["s3:GetBucketLocation"]
    resources = [local.bucket_arn]
  }

  # Load and bench reports go here so they can be read without SSM output limits.
  statement {
    sid       = "WriteServingMetrics"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${local.bucket_arn}/metrics/serving/*"]
  }
}

resource "aws_iam_role_policy" "data" {
  count  = local.count_if_enabled
  name   = "edgarqa-serving-box-data"
  role   = aws_iam_role.box[0].id
  policy = data.aws_iam_policy_document.data.json
}

resource "aws_iam_instance_profile" "box" {
  count = local.count_if_enabled
  name  = "edgarqa-serving-box"
  role  = aws_iam_role.box[0].name
}

resource "aws_instance" "box" {
  count                       = local.count_if_enabled
  ami                         = data.aws_ssm_parameter.al2023[0].value
  instance_type               = var.instance_type
  subnet_id                   = sort(data.aws_subnets.default[0].ids)[0]
  vpc_security_group_ids      = [aws_security_group.box[0].id]
  iam_instance_profile        = aws_iam_instance_profile.box[0].name
  associate_public_ip_address = true # outbound SSM and Docker Hub without a NAT gateway (none, ever)

  metadata_options {
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 1          # host processes only: no container can reach the role
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.volume_gb
    encrypted             = true
    delete_on_termination = true # the corpus is rebuildable from the S3 export
  }

  user_data = <<-EOT
    #!/bin/bash
    set -euxo pipefail
    dnf install -y docker
    systemctl enable --now docker
    curl -LsSf https://astral.sh/uv/0.10.8/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
    mkdir -p /opt/edgarqa /var/lib/edgarqa/pgdata
    touch /opt/edgarqa/bootstrap.done
  EOT

  tags = {
    Name = "edgarqa-serving-box"
  }

  lifecycle {
    ignore_changes = [ami] # a newer AMI must not replace a loaded box
  }
}
