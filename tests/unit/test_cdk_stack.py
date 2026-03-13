"""Unit tests for the Copilot Dispatch CDK stack synthesis and resource configuration.

Uses ``aws_cdk.assertions.Template`` to validate the synthesised CloudFormation
template without requiring AWS credentials.

Requires ``aws-cdk-lib`` (install via ``pip install -r infra/requirements.txt``).
Tests are automatically skipped when the package is not installed.
"""

from __future__ import annotations

import pytest

cdk = pytest.importorskip(
    "aws_cdk",
    reason="aws-cdk-lib not installed — install via 'pip install -r infra/requirements.txt'",
)

from aws_cdk.assertions import Capture, Match, Template  # noqa: E402

from infra.stacks.copilot_dispatch_stack import CopilotDispatchStack  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def template() -> Template:
    """Synthesise the ``CopilotDispatchStack`` and return the assertion template.

    Module-scoped for efficiency — the stack is synthesised once and shared
    across all test functions.
    """
    app = cdk.App(
        context={
            "stack_name": "copilot-dispatch",
            "aws_region": "ap-southeast-2",
            "dynamodb_table_name": "dispatch-runs",
            "ecr_repository_name": "copilot-dispatch",
            "apprunner_cpu": "1024",
            "apprunner_memory": "2048",
            "apprunner_min_size": "1",
            "apprunner_max_size": "1",
        }
    )
    stack = CopilotDispatchStack(
        app,
        "copilot-dispatch",
        env=cdk.Environment(region="ap-southeast-2"),
    )
    return Template.from_stack(stack)


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


class TestStackSynthesis:
    """Verify the stack can be synthesised without errors."""

    def test_stack_synthesizes_without_errors(self) -> None:
        """Stack synthesis should complete without raising any exceptions."""
        app = cdk.App(
            context={
                "stack_name": "copilot-dispatch",
                "aws_region": "ap-southeast-2",
                "dynamodb_table_name": "dispatch-runs",
                "ecr_repository_name": "copilot-dispatch",
                "apprunner_cpu": "1024",
                "apprunner_memory": "2048",
                "apprunner_min_size": "1",
                "apprunner_max_size": "1",
            }
        )
        stack = CopilotDispatchStack(
            app,
            "copilot-dispatch-test",
            env=cdk.Environment(region="ap-southeast-2"),
        )
        # Synthesise — will raise on any error
        cloud_assembly = app.synth()
        assert cloud_assembly is not None
        assert len(cloud_assembly.stacks) == 1


# ---------------------------------------------------------------------------
# DynamoDB
# ---------------------------------------------------------------------------


class TestDynamoDBTable:
    """Verify the DynamoDB table resource configuration."""

    def test_dynamodb_table_created(self, template: Template) -> None:
        """Template should contain an ``AWS::DynamoDB::Table`` resource."""
        template.resource_count_is("AWS::DynamoDB::Table", 1)

    def test_dynamodb_table_key_schema(self, template: Template) -> None:
        """Table partition key should be ``run_id`` of type String."""
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "KeySchema": [
                    {"AttributeName": "run_id", "KeyType": "HASH"},
                ],
                "AttributeDefinitions": [
                    {"AttributeName": "run_id", "AttributeType": "S"},
                ],
            },
        )

    def test_dynamodb_table_billing_mode(self, template: Template) -> None:
        """Table should use on-demand (PAY_PER_REQUEST) billing."""
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {"BillingMode": "PAY_PER_REQUEST"},
        )

    def test_dynamodb_table_ttl(self, template: Template) -> None:
        """Table should have TTL enabled on the ``ttl`` attribute."""
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "TimeToLiveSpecification": {
                    "AttributeName": "ttl",
                    "Enabled": True,
                },
            },
        )

    def test_dynamodb_table_name(self, template: Template) -> None:
        """Table name should match the context value ``dispatch-runs``."""
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {"TableName": "dispatch-runs"},
        )

    def test_dynamodb_table_pitr(self, template: Template) -> None:
        """Table should have point-in-time recovery enabled."""
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "PointInTimeRecoverySpecification": {
                    "PointInTimeRecoveryEnabled": True,
                },
            },
        )

    def test_dynamodb_table_destroy_policy(self, template: Template) -> None:
        """Table should have DeletionPolicy set to Delete (DESTROY)."""
        template.has_resource(
            "AWS::DynamoDB::Table",
            {
                "DeletionPolicy": "Delete",
                "UpdateReplacePolicy": "Delete",
            },
        )


# ---------------------------------------------------------------------------
# ECR Repository
# ---------------------------------------------------------------------------


class TestECRRepository:
    """Verify the ECR repository resource configuration."""

    def test_ecr_repository_created(self, template: Template) -> None:
        """Template should contain an ``AWS::ECR::Repository`` resource."""
        template.resource_count_is("AWS::ECR::Repository", 1)

    def test_ecr_repository_name(self, template: Template) -> None:
        """Repository name should match the context value ``copilot-dispatch``."""
        template.has_resource_properties(
            "AWS::ECR::Repository",
            {"RepositoryName": "copilot-dispatch"},
        )

    def test_ecr_image_scan_on_push(self, template: Template) -> None:
        """Image scanning on push should be enabled."""
        template.has_resource_properties(
            "AWS::ECR::Repository",
            {
                "ImageScanningConfiguration": {"ScanOnPush": True},
            },
        )

    def test_ecr_lifecycle_rules(self, template: Template) -> None:
        """Lifecycle policy should include untagged expiry and keep-last-N rules."""
        template.has_resource_properties(
            "AWS::ECR::Repository",
            {
                "LifecyclePolicy": {
                    "LifecyclePolicyText": Match.string_like_regexp(
                        r".*Expire untagged images.*Keep last 10 images.*"
                    ),
                },
            },
        )

    def test_ecr_destroy_policy(self, template: Template) -> None:
        """Repository should have DeletionPolicy set to Delete (DESTROY)."""
        template.has_resource(
            "AWS::ECR::Repository",
            {
                "DeletionPolicy": "Delete",
                "UpdateReplacePolicy": "Delete",
            },
        )


# ---------------------------------------------------------------------------
# AppRunner Service
# ---------------------------------------------------------------------------


class TestAppRunnerService:
    """Verify the AppRunner service resource configuration."""

    def test_apprunner_service_created(self, template: Template) -> None:
        """Template should contain an ``AWS::AppRunner::Service`` resource."""
        template.resource_count_is("AWS::AppRunner::Service", 1)

    def test_apprunner_service_name(self, template: Template) -> None:
        """Service name should be ``dispatch-api``."""
        template.has_resource_properties(
            "AWS::AppRunner::Service",
            {"ServiceName": "dispatch-api"},
        )

    def test_apprunner_cpu_and_memory(self, template: Template) -> None:
        """Service should have 1024 CPU and 2048 memory."""
        template.has_resource_properties(
            "AWS::AppRunner::Service",
            {
                "InstanceConfiguration": {
                    "Cpu": "1024",
                    "Memory": "2048",
                },
            },
        )

    def test_apprunner_health_check(self, template: Template) -> None:
        """Health check should target ``/health`` via HTTP with 10s interval."""
        template.has_resource_properties(
            "AWS::AppRunner::Service",
            {
                "HealthCheckConfiguration": {
                    "Protocol": "HTTP",
                    "Path": "/health",
                    "Interval": 10,
                    "Timeout": 5,
                    "HealthyThreshold": 1,
                    "UnhealthyThreshold": 5,
                },
            },
        )

    def test_apprunner_port(self, template: Template) -> None:
        """Image configuration should expose port 8000."""
        template.has_resource_properties(
            "AWS::AppRunner::Service",
            {
                "SourceConfiguration": {
                    "ImageRepository": {
                        "ImageConfiguration": {
                            "Port": "8000",
                        },
                    },
                },
            },
        )

    def test_apprunner_auto_deployment_enabled(self, template: Template) -> None:
        """Auto-deployment should be enabled for ECR push triggers."""
        template.has_resource_properties(
            "AWS::AppRunner::Service",
            {
                "SourceConfiguration": {
                    "AutoDeploymentsEnabled": True,
                },
            },
        )

    def test_apprunner_ecr_image_type(self, template: Template) -> None:
        """Image repository type should be ECR."""
        template.has_resource_properties(
            "AWS::AppRunner::Service",
            {
                "SourceConfiguration": {
                    "ImageRepository": {
                        "ImageRepositoryType": "ECR",
                    },
                },
            },
        )

    def test_apprunner_environment_variables(self, template: Template) -> None:
        """Non-sensitive environment variables should be present."""
        template.has_resource_properties(
            "AWS::AppRunner::Service",
            {
                "SourceConfiguration": {
                    "ImageRepository": {
                        "ImageConfiguration": {
                            "RuntimeEnvironmentVariables": Match.array_with(
                                [
                                    {
                                        "Name": "DISPATCH_APP_ENV",
                                        "Value": "production",
                                    },
                                    {
                                        "Name": "DISPATCH_DYNAMODB_TABLE_NAME",
                                        "Value": "dispatch-runs",
                                    },
                                    {
                                        "Name": "DISPATCH_LOG_LEVEL",
                                        "Value": "INFO",
                                    },
                                ]
                            ),
                        },
                    },
                },
            },
        )

    def test_apprunner_secrets_injected(self, template: Template) -> None:
        """Secrets Manager ARNs should be referenced in RuntimeEnvironmentSecrets."""
        template.has_resource_properties(
            "AWS::AppRunner::Service",
            {
                "SourceConfiguration": {
                    "ImageRepository": {
                        "ImageConfiguration": {
                            "RuntimeEnvironmentSecrets": Match.array_with(
                                [
                                    {
                                        "Name": "DISPATCH_API_KEY",
                                        "Value": Match.any_value(),
                                    },
                                    {
                                        "Name": "DISPATCH_WEBHOOK_SECRET",
                                        "Value": Match.any_value(),
                                    },
                                    {
                                        "Name": "GITHUB_PAT",
                                        "Value": Match.any_value(),
                                    },
                                ]
                            ),
                        },
                    },
                },
            },
        )


# ---------------------------------------------------------------------------
# IAM Roles
# ---------------------------------------------------------------------------


class TestIAMRoles:
    """Verify IAM role configuration for AppRunner."""

    def test_iam_instance_role_has_dynamodb_access(self, template: Template) -> None:
        """Instance role policy should include DynamoDB CRUD actions."""
        template.has_resource_properties(
            "AWS::IAM::Policy",
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Sid": "DynamoDBAccess",
                                    "Effect": "Allow",
                                    "Action": [
                                        "dynamodb:GetItem",
                                        "dynamodb:PutItem",
                                        "dynamodb:UpdateItem",
                                        "dynamodb:Query",
                                        "dynamodb:Scan",
                                        "dynamodb:DeleteItem",
                                    ],
                                }
                            ),
                        ]
                    ),
                },
            },
        )

    def test_iam_instance_role_has_secrets_manager_access(
        self, template: Template
    ) -> None:
        """Instance role policy should include Secrets Manager GetSecretValue."""
        template.has_resource_properties(
            "AWS::IAM::Policy",
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Sid": "SecretsManagerAccess",
                                    "Effect": "Allow",
                                    "Action": "secretsmanager:GetSecretValue",
                                }
                            ),
                        ]
                    ),
                },
            },
        )

    def test_iam_ecr_access_role_created(self, template: Template) -> None:
        """An ECR access role for ``build.apprunner.amazonaws.com`` should exist."""
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "AssumeRolePolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            {
                                "Action": "sts:AssumeRole",
                                "Effect": "Allow",
                                "Principal": {
                                    "Service": "build.apprunner.amazonaws.com"
                                },
                            },
                        ]
                    ),
                },
            },
        )

    def test_ecr_access_role_has_managed_policy(self, template: Template) -> None:
        """ECR access role should have the AppRunner ECR managed policy attached."""
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "ManagedPolicyArns": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Fn::Join": Match.array_with(
                                    [
                                        "",
                                        Match.array_with(
                                            [
                                                Match.string_like_regexp(
                                                    r".*AWSAppRunnerServicePolicyForECRAccess.*"
                                                ),
                                            ]
                                        ),
                                    ]
                                ),
                            }
                        ),
                    ]
                ),
            },
        )


# ---------------------------------------------------------------------------
# Auto-Scaling Configuration
# ---------------------------------------------------------------------------


class TestAutoScaling:
    """Verify the AppRunner auto-scaling configuration."""

    def test_auto_scaling_configuration_created(self, template: Template) -> None:
        """Template should contain an auto-scaling configuration resource."""
        template.resource_count_is("AWS::AppRunner::AutoScalingConfiguration", 1)

    def test_auto_scaling_configuration(self, template: Template) -> None:
        """Auto-scaling should be configured with min 1, max 1, concurrency 100."""
        template.has_resource_properties(
            "AWS::AppRunner::AutoScalingConfiguration",
            {
                "MaxConcurrency": 100,
                "MaxSize": 1,
                "MinSize": 1,
            },
        )


# ---------------------------------------------------------------------------
# CloudFormation Outputs
# ---------------------------------------------------------------------------


class TestCloudFormationOutputs:
    """Verify the CloudFormation outputs are present."""

    def test_apprunner_service_url_output(self, template: Template) -> None:
        """``AppRunnerServiceUrl`` output should be present."""
        template.has_output(
            "AppRunnerServiceUrl",
            {
                "Description": "HTTPS URL of the AppRunner service",
            },
        )

    def test_dynamodb_table_name_output(self, template: Template) -> None:
        """``DynamoDbTableName`` output should be present."""
        template.has_output(
            "DynamoDbTableName",
            {
                "Description": "Name of the DynamoDB table for run records",
            },
        )

    def test_ecr_repository_uri_output(self, template: Template) -> None:
        """``EcrRepositoryUri`` output should be present."""
        template.has_output(
            "EcrRepositoryUri",
            {
                "Description": "URI of the ECR repository for Docker push",
            },
        )
