variable "aws_region" {
  description = "AWS region for every resource in this configuration."
  type        = string
  default     = "us-east-1"

  validation {
    condition     = can(regex("^[a-z]{2}(-gov)?-[a-z]+-[0-9]$", var.aws_region))
    error_message = "aws_region must be an AWS region name such as us-east-1."
  }
}

variable "name_prefix" {
  description = "Prefix for resource names. Lowercase letters, digits and hyphens only."
  type        = string
  default     = "aica"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}$", var.name_prefix))
    error_message = "name_prefix must be 2-31 lowercase alphanumeric or hyphen characters."
  }
}

variable "vpc_id" {
  description = "VPC that hosts the database and the application runtime."
  type        = string

  validation {
    condition     = can(regex("^vpc-[0-9a-f]{8,17}$", var.vpc_id))
    error_message = "vpc_id must be an existing VPC id (vpc-...)."
  }
}

variable "private_subnet_ids" {
  description = "Two or more private subnets in different availability zones."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "At least two private subnets are required for a managed database."
  }

  validation {
    condition     = alltrue([for id in var.private_subnet_ids : can(regex("^subnet-[0-9a-f]{8,17}$", id))])
    error_message = "Every private_subnet_ids entry must be a subnet id (subnet-...)."
  }
}

variable "allowed_ingress_cidrs" {
  description = "CIDR blocks allowed to reach the application container port from inside the VPC."
  type        = list(string)
  default     = ["10.0.0.0/8"]

  validation {
    condition     = length(var.allowed_ingress_cidrs) > 0
    error_message = "allowed_ingress_cidrs must contain at least one CIDR block."
  }

  validation {
    condition     = alltrue([for cidr in var.allowed_ingress_cidrs : can(cidrhost(cidr, 0))])
    error_message = "Every allowed_ingress_cidrs entry must be a valid IPv4 or IPv6 CIDR block."
  }
}

variable "db_instance_class" {
  description = "RDS instance class for PostgreSQL."
  type        = string
  default     = "db.t4g.medium"

  validation {
    condition     = can(regex("^db\\.[a-z0-9]+\\.[a-z0-9]+$", var.db_instance_class))
    error_message = "db_instance_class must look like db.t4g.medium."
  }
}

variable "db_allocated_storage" {
  description = "Allocated database storage in GiB."
  type        = number
  default     = 100

  validation {
    condition     = var.db_allocated_storage >= 20 && var.db_allocated_storage <= 16384
    error_message = "db_allocated_storage must be between 20 and 16384 GiB."
  }
}

variable "db_name" {
  description = "Initial database name."
  type        = string
  default     = "aica"

  validation {
    condition     = can(regex("^[a-z][a-z0-9_]{1,62}$", var.db_name))
    error_message = "db_name must start with a letter and contain only lowercase letters, digits and underscores."
  }
}

variable "db_username" {
  description = "Master username. The password is generated and stored by RDS in Secrets Manager."
  type        = string
  default     = "aica"

  validation {
    condition     = can(regex("^[a-z][a-z0-9_]{1,62}$", var.db_username))
    error_message = "db_username must start with a letter and contain only lowercase letters, digits and underscores."
  }
}

variable "db_multi_az" {
  description = "Enable a synchronous standby in a second availability zone."
  type        = bool
  default     = true
}

variable "db_backup_retention_days" {
  description = "Automated backup retention in days."
  type        = number
  default     = 7

  validation {
    condition     = var.db_backup_retention_days >= 0 && var.db_backup_retention_days <= 35
    error_message = "db_backup_retention_days must be between 0 and 35."
  }
}

variable "app_image" {
  description = "Container image for the web and worker runtime."
  type        = string

  validation {
    condition     = can(regex("^[^[:space:]]+/[^[:space:]]+:[^[:space:]]+$", var.app_image))
    error_message = "app_image must be a tagged image reference (repository:tag)."
  }
}

variable "app_secret_key_secret_arn" {
  description = "Secrets Manager ARN holding the Flask SECRET_KEY. The value is never stored in Terraform."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:.+$", var.app_secret_key_secret_arn))
    error_message = "app_secret_key_secret_arn must be a Secrets Manager secret ARN."
  }
}

variable "database_url_secret_arn" {
  description = "Secrets Manager ARN holding the full DATABASE_URL for the application."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:.+$", var.database_url_secret_arn))
    error_message = "database_url_secret_arn must be a Secrets Manager secret ARN."
  }
}

variable "app_cpu" {
  description = "Fargate CPU units for the web and worker task (256, 512, 1024, 2048 or 4096)."
  type        = number
  default     = 512

  validation {
    condition     = contains([256, 512, 1024, 2048, 4096], var.app_cpu)
    error_message = "app_cpu must be one of 256, 512, 1024, 2048 or 4096."
  }
}

variable "app_memory" {
  description = "Fargate memory in MiB. Must be a valid size for app_cpu."
  type        = number
  default     = 1024

  validation {
    condition     = var.app_memory >= 512 && var.app_memory <= 8192
    error_message = "app_memory must be between 512 and 8192 MiB."
  }
}

variable "app_container_port" {
  description = "Container port the web runtime listens on."
  type        = number
  default     = 5000

  validation {
    condition     = var.app_container_port >= 1 && var.app_container_port <= 65535
    error_message = "app_container_port must be between 1 and 65535."
  }
}

variable "web_desired_count" {
  description = "Number of web tasks."
  type        = number
  default     = 2

  validation {
    condition     = var.web_desired_count >= 1 && var.web_desired_count <= 50
    error_message = "web_desired_count must be between 1 and 50."
  }
}

variable "worker_desired_count" {
  description = "Number of job-queue worker tasks. Zero disables the worker."
  type        = number
  default     = 1

  validation {
    condition     = var.worker_desired_count >= 0 && var.worker_desired_count <= 50
    error_message = "worker_desired_count must be between 0 and 50."
  }
}

variable "log_retention_days" {
  description = "CloudWatch log retention for the runtime."
  type        = number
  default     = 30

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "log_retention_days must be a CloudWatch-supported retention value."
  }
}
