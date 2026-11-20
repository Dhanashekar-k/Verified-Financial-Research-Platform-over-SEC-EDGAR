# Cost guardrails that are free to create (04_aws_and_cost_plan.md §2 items 2 and 4).
# Cost Explorer, Budgets and Anomaly Detection are global APIs whose endpoint is in us-east-1; the provider region (us-east-2) is unaffected.

# One monitor per AWS service: a spike in any single service raises an anomaly.
resource "aws_ce_anomaly_monitor" "service" {
  name              = "edgarqa-service-monitor"
  monitor_type      = "DIMENSIONAL"
  monitor_dimension = "SERVICE"
}

# Daily summary email. Daily frequency needs an EMAIL subscriber; the $1 threshold keeps noise down
# without hiding anything that matters on a project with an $80 monthly budget.
resource "aws_ce_anomaly_subscription" "daily" {
  name             = "edgarqa-daily-anomalies"
  frequency        = "DAILY"
  monitor_arn_list = [aws_ce_anomaly_monitor.service.arn]

  subscriber {
    type    = "EMAIL"
    address = var.alert_email
  }

  threshold_expression {
    dimension {
      key           = "ANOMALY_TOTAL_IMPACT_ABSOLUTE"
      match_options = ["GREATER_THAN_OR_EQUAL"]
      values        = ["1"]
    }
  }
}

# Only the Organization's management account may activate cost allocation tags; a member account gets
# AccessDeniedException. Off by default here; activate `project` from the management account instead.
# Lets Cost Explorer group and filter by project (used by `make cost-today`).
# AWS only accepts a tag key it has already seen on a resource; it appears in Cost Explorer ~24 h after activation.
resource "aws_ce_cost_allocation_tag" "project" {
  count   = var.activate_cost_allocation_tag ? 1 : 0
  tag_key = var.cost_allocation_tag_key
  status  = "Active"
}
