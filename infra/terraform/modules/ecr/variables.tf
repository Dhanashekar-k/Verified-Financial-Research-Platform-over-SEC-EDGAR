variable "name" {
  description = "ECR repository name."
  type        = string
  default     = "edgarqa-emr"
}

variable "account_id" {
  description = "AWS account ID, scopes the EMR Serverless pull permission."
  type        = string
}

variable "region" {
  description = "AWS region of the EMR Serverless application that pulls the image."
  type        = string
}

variable "keep_images" {
  description = "How many tagged images to keep; older ones expire."
  type        = number
  default     = 10
}
