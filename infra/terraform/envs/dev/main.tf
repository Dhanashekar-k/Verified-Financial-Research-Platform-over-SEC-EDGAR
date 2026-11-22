module "lake" {
  source     = "../../modules/lake"
  account_id = var.account_id
}

module "budgets" {
  source      = "../../modules/budgets"
  alert_email = var.alert_email
}

module "cost_guardrails" {
  source      = "../../modules/cost_guardrails"
  alert_email = var.alert_email
}

module "github_oidc" {
  count       = 0
  source      = "../../modules/github_oidc"
  github_repo = var.github_repo
}

module "ecr" {
  source     = "../../modules/ecr"
  account_id = var.account_id
  region     = var.region
}

module "glue" {
  source              = "../../modules/glue"
  account_id          = var.account_id
  region              = var.region
  lake_bucket_name    = module.lake.bucket_name
  glue_database_names = values(module.lake.glue_database_names)
  smoke_databases     = var.smoke_databases
}

module "athena" {
  source           = "../../modules/athena"
  lake_bucket_name = module.lake.bucket_name
}

module "emr" {
  source              = "../../modules/emr"
  account_id          = var.account_id
  region              = var.region
  lake_bucket_name    = module.lake.bucket_name
  glue_database_names = values(module.lake.glue_database_names)
  image_uri           = var.emr_image_uri
  create_application  = var.create_emr_application
}

module "serving_db" {
  source           = "../../modules/serving_db"
  enabled          = var.serving_db_enabled
  lake_bucket_name = module.lake.bucket_name
}
