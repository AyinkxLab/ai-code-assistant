# Terraform module: managed PostgreSQL and app runtime (#708)

Creates:

- an encrypted, private RDS for PostgreSQL 16 instance whose master password is
  generated and stored by RDS in Secrets Manager;
- two security groups (runtime and database), scoped to the supplied VPC and
  private subnets;
- an ECS Fargate cluster, task definitions and services for the web process and
  the job-queue worker;
- a CloudWatch log group and a least-privilege task execution role that can read
  only the two supplied Secrets Manager ARNs.

No credential enters Terraform. `DATABASE_URL` and `SECRET_KEY` are injected
from Secrets Manager; see [the module guide](../../docs/terraform.md).

## Use

```bash
cd deploy/terraform
cp terraform.tfvars.example terraform.tfvars
# edit the placeholders, then:
terraform init
terraform plan
terraform apply
```

## Test

```bash
python -m pytest tests/test_deploy_terraform.py -q
```

The tests are static because `terraform` is not installed in this environment.
They do not run `terraform validate`.
