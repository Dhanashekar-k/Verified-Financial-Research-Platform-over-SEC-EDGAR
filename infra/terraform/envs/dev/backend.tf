terraform {
  backend "s3" {
    # bucket is supplied at init time (backends cannot use variables):
    #   terraform init -backend-config="bucket=edgarqa-tfstate-<account-id>"
    key          = "edgarqa/dev/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true # S3-native locking, needs Terraform >= 1.10
  }
}
