output "instance_id" {
  description = "The serving box instance id (empty when disabled)."
  value       = var.enabled ? aws_instance.box[0].id : null
}

output "role_arn" {
  value = var.enabled ? aws_iam_role.box[0].arn : null
}
