"""Static checks for the Terraform module (#708).

`terraform` is not part of the Python toolchain and is not installed in CI, so
these tests read the HCL as text. They pin the module contract (required
variables, defaults, validated inputs, outputs) and the safety properties that
matter most: no inline credentials, an encrypted private database, and secrets
read from Secrets Manager.
"""

from __future__ import annotations

import re
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1] / "deploy" / "terraform"


def _text(name: str) -> str:
    return (MODULE_DIR / name).read_text(encoding="utf-8")


def _variables() -> dict[str, str]:
    body = _text("variables.tf")
    blocks = re.findall(r'variable "([^"]+)" \{(.*?)\n\}', body, re.DOTALL)
    assert blocks, "variables.tf declares no variables"
    return dict(blocks)


def test_required_variables_have_no_default():
    variables = _variables()
    for name in (
        "vpc_id",
        "private_subnet_ids",
        "app_image",
        "app_secret_key_secret_arn",
        "database_url_secret_arn",
    ):
        assert name in variables
        assert "default" not in variables[name], f"{name} must be required"


def test_documented_defaults_are_stable():
    variables = _variables()
    expected = {
        "aws_region": '"us-east-1"',
        "db_instance_class": '"db.t4g.medium"',
        "db_allocated_storage": "100",
        "app_cpu": "512",
        "app_memory": "1024",
        "web_desired_count": "2",
        "worker_desired_count": "1",
    }
    for name, value in expected.items():
        assert re.search(rf"default\s*=\s*{re.escape(value)}", variables[name]), name


def test_every_input_validation_has_a_condition_and_message():
    variables = _variables()
    for name in (
        "aws_region",
        "name_prefix",
        "vpc_id",
        "private_subnet_ids",
        "allowed_ingress_cidrs",
        "db_instance_class",
        "db_allocated_storage",
        "db_name",
        "db_username",
        "db_backup_retention_days",
        "app_image",
        "app_secret_key_secret_arn",
        "database_url_secret_arn",
        "app_cpu",
        "app_memory",
        "app_container_port",
        "web_desired_count",
        "worker_desired_count",
        "log_retention_days",
    ):
        body = variables[name]
        assert "validation {" in body, f"{name} has no validation block"
        assert "condition" in body and "error_message" in body, name
        assert re.search(r'error_message\s*=\s*"[^"]+"', body), name


def test_region_validation_rejects_invalid_regions():
    body = _variables()["aws_region"]
    pattern = re.search(r'can\(regex\("([^"]+)"', body).group(1)
    assert re.fullmatch(pattern, "us-east-1")
    assert re.fullmatch(pattern, "eu-central-1")
    for invalid in ("US-EAST-1", "us_east_1", "us-east", "../etc/passwd", ""):
        assert not re.fullmatch(pattern, invalid), invalid


def test_outputs_cover_database_runtime_and_handles():
    outputs = _text("outputs.tf")
    for name in (
        "database_endpoint",
        "database_master_user_secret_arn",
        "ecs_cluster_name",
        "web_service_name",
        "worker_service_name",
        "app_security_group_id",
        "database_security_group_id",
        "log_group_name",
    ):
        assert re.search(rf'output "{name}"', outputs), name
    assert "sensitive" not in outputs


def test_database_is_private_encrypted_and_passwordless_in_config():
    main = _text("main.tf")
    assert "manage_master_user_password = true" in main
    assert "storage_encrypted = true" in main
    assert "publicly_accessible    = false" in main
    assert "deletion_protection     = true" in main
    assert "multi_az               = var.db_multi_az" in main
    assert not re.search(r"(?i)\bpassword\s*=\s*\"", main), "inline password found"
    assert not re.search(r"(?i)master_password\s*=", main)


def test_runtime_reads_secrets_from_secrets_manager():
    main = _text("main.tf")
    assert "aws_iam_policy_document" in main
    assert '"secretsmanager:GetSecretValue"' in main
    assert "valueFrom = var.app_secret_key_secret_arn" in main
    assert "valueFrom = var.database_url_secret_arn" in main
    assert "assign_public_ip = false" in main


def test_region_is_parameterized_through_the_provider():
    provider = _text("versions.tf")
    assert 'required_version = ">= 1.6.0"' in provider
    assert "region = var.aws_region" in provider


def test_example_usage_lists_every_required_input():
    example = _text("terraform.tfvars.example")
    for name in (
        "aws_region",
        "vpc_id",
        "private_subnet_ids",
        "app_image",
        "app_secret_key_secret_arn",
        "database_url_secret_arn",
    ):
        assert re.search(rf"^{name}\s*=", example, re.MULTILINE), name
    assert not re.search(r"(?i)password\s*=", example)
