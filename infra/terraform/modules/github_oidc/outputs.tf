output "role_arn" {
  description = "ARN of the role GitHub Actions assumes. Set it as a repo variable for workflows."
  value       = aws_iam_role.github_actions.arn
}
