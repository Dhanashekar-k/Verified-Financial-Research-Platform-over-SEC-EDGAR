output "workgroup_name" {
  description = "Athena workgroup with the scan limit."
  value       = aws_athena_workgroup.edgarqa.name
}
