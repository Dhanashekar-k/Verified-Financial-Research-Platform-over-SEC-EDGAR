output "repository_url" {
  description = "Repository URL, without a tag (append :<git-sha>)."
  value       = aws_ecr_repository.emr.repository_url
}

output "repository_name" {
  description = "Repository name."
  value       = aws_ecr_repository.emr.name
}
