output "database_endpoint" {
  description = "Host:port of the managed PostgreSQL instance."
  value       = aws_db_instance.this.endpoint
}

output "database_master_user_secret_arn" {
  description = "Secrets Manager ARN for the RDS-generated master password."
  value       = aws_db_instance.this.master_user_secret[0].secret_arn
}

output "ecs_cluster_name" {
  description = "ECS cluster that runs the web and worker services."
  value       = aws_ecs_cluster.this.name
}

output "web_service_name" {
  description = "ECS service name for the web runtime."
  value       = aws_ecs_service.web.name
}

output "worker_service_name" {
  description = "ECS service name for the job-queue worker."
  value       = aws_ecs_service.worker.name
}

output "app_security_group_id" {
  description = "Security group attached to runtime tasks."
  value       = aws_security_group.app.id
}

output "database_security_group_id" {
  description = "Security group attached to the database."
  value       = aws_security_group.database.id
}

output "log_group_name" {
  description = "CloudWatch log group for runtime logs."
  value       = aws_cloudwatch_log_group.app.name
}
