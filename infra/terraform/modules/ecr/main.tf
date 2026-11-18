# Holds the custom EMR Serverless image (infra/emr/Dockerfile, ADR-024). Pushing is done by the
# owner with `make emr-image-push`; nothing in this repo pushes on its own.

resource "aws_ecr_repository" "emr" {
  name = var.name

  # Tags are git SHAs, so an existing tag must never be overwritten.
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "emr" {
  repository = aws_ecr_repository.emr.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep the last ${var.keep_images} images; the rest are build leftovers."
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = var.keep_images
        }
        action = { type = "expire" }
      }
    ]
  })
}

# EMR Serverless pulls the image with its own service principal, so the repository must allow it.
# Scoped to applications in this account and region; only read actions.
data "aws_iam_policy_document" "emr_pull" {
  statement {
    sid    = "EmrServerlessPull"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["emr-serverless.amazonaws.com"]
    }

    # BatchGetImage and GetDownloadUrlForLayer fetch the layers; DescribeImages lets EMR check the tag.
    actions = [
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
    ]

    condition {
      test     = "StringLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:emr-serverless:${var.region}:${var.account_id}:/applications/*"]
    }
  }
}

resource "aws_ecr_repository_policy" "emr" {
  repository = aws_ecr_repository.emr.name
  policy     = data.aws_iam_policy_document.emr_pull.json
}
