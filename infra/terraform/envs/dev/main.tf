module "lake" {
  source     = "../../modules/lake"
  account_id = var.account_id
}

module "budgets" {
  source      = "../../modules/budgets"
  alert_email = var.alert_email
}

module "github_oidc" {
  source      = "../../modules/github_oidc"
  github_repo = var.github_repo
}

module "ecr" {
  source     = "../../modules/ecr"
  account_id = var.account_id
  region     = var.region
}

module "emr" {
  source              = "../../modules/emr"
  account_id          = var.account_id
  region              = var.region
  lake_bucket_name    = module.lake.bucket_name
  glue_database_names = values(module.lake.glue_database_names)
  image_uri           = var.emr_image_uri
}
