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
