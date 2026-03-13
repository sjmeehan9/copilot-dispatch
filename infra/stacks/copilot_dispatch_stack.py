"""AWS CDK stack for the Copilot Dispatch infrastructure.

Provisions all AWS resources required to run the Copilot Dispatch API service in
production:

- **DynamoDB** table (``dispatch-runs``) for agent run record persistence.
- **ECR** repository for the API container image.
- **IAM** roles granting AppRunner access to DynamoDB and Secrets Manager.
- **AppRunner** service pulling from ECR with auto-deployment enabled.
- **Auto-scaling configuration** supporting scale-to-zero for cost efficiency.
- **Secrets Manager** references injected as environment variables into the
  AppRunner container at runtime.

All resource names and sizing parameters are read from CDK context values
(defined in ``infra/cdk.json``) so the stack can be replicated for a staging
environment by overriding context values at synth time.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_apprunner as apprunner
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct


class CopilotDispatchStack(Stack):
    """CloudFormation stack provisioning the complete Copilot Dispatch infrastructure.

    The stack creates the following resources:

    1. A DynamoDB table with on-demand billing and TTL for run record storage.
    2. An ECR repository with image scanning and lifecycle rules.
    3. An IAM instance role authorising AppRunner to access DynamoDB and
       Secrets Manager.
    4. An IAM access role authorising AppRunner to pull images from ECR.
    5. An AppRunner auto-scaling configuration (scale-to-zero capable).
    6. An AppRunner service configured with ECR image source, health checks,
       and environment variables sourced from Secrets Manager.

    All configurable values are read from CDK context to support environment
    parameterisation.

    Args:
        scope: The CDK app or parent construct.
        construct_id: Logical identifier for this stack.
        **kwargs: Additional ``Stack`` keyword arguments (e.g. ``env``).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # -- Context values --------------------------------------------------
        table_name: str = (
            self.node.try_get_context("dynamodb_table_name") or "dispatch-runs"
        )
        ecr_repo_name: str = (
            self.node.try_get_context("ecr_repository_name") or "copilot-dispatch"
        )
        github_owner: str = self.node.try_get_context("github_owner") or "sjmeehan9"
        github_repo: str = (
            self.node.try_get_context("github_repo") or "copilot-dispatch"
        )
        apprunner_cpu: str = self.node.try_get_context("apprunner_cpu") or "1024"
        apprunner_memory: str = self.node.try_get_context("apprunner_memory") or "2048"
        apprunner_min_size: str = self.node.try_get_context("apprunner_min_size") or "1"
        apprunner_max_size: str = self.node.try_get_context("apprunner_max_size") or "1"
        aws_region: str = self.node.try_get_context("aws_region") or "ap-southeast-2"

        # Externally reachable HTTPS base URL of the AppRunner service for
        # constructing callback URLs (e.g. api_result_url).  Must be set
        # after the initial deployment once the service URL is known.
        # Example: "https://<hash>.ap-southeast-2.awsapprunner.com"
        service_url: str | None = self.node.try_get_context("service_url")

        # When False, skip AppRunner + auto-scaling resources so the ECR
        # repository can be populated before the service is created.
        # Usage:  cdk deploy -c deploy_apprunner=false
        deploy_apprunner: bool = (
            self.node.try_get_context("deploy_apprunner") != "false"
        )

        # -- DynamoDB table ---------------------------------------------------
        table = self._create_dynamodb_table(table_name)

        # -- ECR repository ---------------------------------------------------
        repository = self._create_ecr_repository(ecr_repo_name)

        # -- IAM roles --------------------------------------------------------
        instance_role = self._create_instance_role(table, aws_region)
        ecr_access_role = self._create_ecr_access_role()

        # -- Secrets Manager references ---------------------------------------
        # These secrets were provisioned manually in Phase 1 (Component 1.1).
        # AppRunner requires full ARNs (including suffix) for secrets, so we
        # attempt to resolve them via boto3 at synth time. We fall back to
        # partial ARNs to allow tests to synthesize cleanly without AWS credentials.
        api_key_secret = secretsmanager.Secret.from_secret_complete_arn(
            self, "ApiKeySecret", self._get_secret_arn("dispatch/api-key", aws_region)
        )
        github_pat_secret = secretsmanager.Secret.from_secret_complete_arn(
            self,
            "GitHubPatSecret",
            self._get_secret_arn("dispatch/github-pat", aws_region),
        )
        webhook_secret = secretsmanager.Secret.from_secret_complete_arn(
            self,
            "WebhookSecret",
            self._get_secret_arn("dispatch/webhook-secret", aws_region),
        )
        openai_key_secret = secretsmanager.Secret.from_secret_complete_arn(
            self,
            "OpenAIApiKeySecret",
            self._get_secret_arn("dispatch/openai-api-key", aws_region),
        )

        # -- AppRunner (conditional) ------------------------------------------
        # On the initial deployment the ECR repository will be empty, so
        # AppRunner cannot start.  Pass ``-c deploy_apprunner=false`` on
        # the first deploy, push the Docker image, then deploy again
        # without the flag to create the AppRunner service.
        if deploy_apprunner:
            # -- Auto-scaling configuration -----------------------------------
            auto_scaling = self._create_auto_scaling_configuration(
                int(apprunner_min_size),
                int(apprunner_max_size),
            )

            # -- AppRunner service --------------------------------------------
            service = self._create_apprunner_service(
                repository=repository,
                ecr_access_role=ecr_access_role,
                instance_role=instance_role,
                auto_scaling=auto_scaling,
                api_key_secret=api_key_secret,
                github_pat_secret=github_pat_secret,
                webhook_secret=webhook_secret,
                openai_key_secret=openai_key_secret,
                table_name=table_name,
                aws_region=aws_region,
                github_owner=github_owner,
                github_repo=github_repo,
                cpu=apprunner_cpu,
                memory=apprunner_memory,
                service_url=service_url,
            )

            CfnOutput(
                self,
                "AppRunnerServiceUrl",
                value=cdk.Fn.join("", ["https://", service.attr_service_url]),
                description="HTTPS URL of the AppRunner service",
            )
        CfnOutput(
            self,
            "DynamoDbTableName",
            value=table.table_name,
            description="Name of the DynamoDB table for run records",
        )
        CfnOutput(
            self,
            "EcrRepositoryUri",
            value=repository.repository_uri,
            description="URI of the ECR repository for Docker push",
        )

    # -- Resource creation methods -------------------------------------------

    def _get_secret_arn(self, secret_name: str, region: str) -> str:
        """Resolve the full ARN of a secret via boto3 if available.

        AppRunner KeyValuePairProperty requires the full secret ARN including
        the 6-character random suffix, which CDK's `from_secret_name_v2` omits.
        If resolution fails (e.g. during offline unit tests), falls back to
        a partial ARN.

        Args:
            secret_name: The name of the secret to look up.
            region: The AWS region.

        Returns:
            The complete ARN if found, else a partial ARN.
        """
        import os

        import boto3
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

        fallback_arn = (
            f"arn:aws:secretsmanager:{region}:{self.account}:secret:{secret_name}"
        )

        try:
            client = boto3.client("secretsmanager", region_name=region)
            response = client.describe_secret(SecretId=secret_name)
            return response.get("ARN", fallback_arn)
        except (BotoCoreError, ClientError, NoCredentialsError):
            return fallback_arn

    def _create_dynamodb_table(self, table_name: str) -> dynamodb.Table:
        """Create the DynamoDB table for agent run record storage.

        Uses on-demand billing (pay-per-request) to avoid capacity planning for
        an intermittently-used service.  Point-in-time recovery is enabled for
        data safety, and TTL is configured on the ``ttl`` attribute so records
        expire automatically after the configured retention period.

        ``RemovalPolicy.DESTROY`` allows CloudFormation to delete the table
        when the stack is destroyed — suitable for development and early
        production where data can be recreated.

        Args:
            table_name: The DynamoDB table name.

        Returns:
            The DynamoDB ``Table`` construct.
        """
        return dynamodb.Table(
            self,
            "RunsTable",
            table_name=table_name,
            partition_key=dynamodb.Attribute(
                name="run_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

    def _create_ecr_repository(self, repo_name: str) -> ecr.Repository:
        """Create the ECR repository for the API container image.

        Image scanning on push detects vulnerabilities early.  Lifecycle rules
        limit storage costs by removing untagged images after 7 days and
        retaining only the 10 most recent tagged images.

        ``RemovalPolicy.DESTROY`` allows CloudFormation to delete the repository
        when the stack is destroyed — suitable for development and early
        production where data can be recreated.

        Args:
            repo_name: The ECR repository name.

        Returns:
            The ECR ``Repository`` construct.
        """
        repo = ecr.Repository(
            self,
            "Repository",
            repository_name=repo_name,
            image_scan_on_push=True,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Remove untagged images after 7 days to reclaim storage.
        # Lower priority number = higher precedence.  The UNTAGGED rule must
        # have a lower priority number than the ANY rule (CDK requirement).
        repo.add_lifecycle_rule(
            description="Expire untagged images after 7 days",
            max_image_age=cdk.Duration.days(7),
            rule_priority=1,
            tag_status=ecr.TagStatus.UNTAGGED,
        )

        # Keep last 10 images (ANY tag status must have the highest priority
        # number — i.e. lowest precedence).
        repo.add_lifecycle_rule(
            description="Keep last 10 images",
            max_image_count=10,
            rule_priority=2,
            tag_status=ecr.TagStatus.ANY,
        )

        return repo

    def _create_instance_role(
        self,
        table: dynamodb.Table,
        aws_region: str,
    ) -> iam.Role:
        """Create the IAM instance role assumed by AppRunner task containers.

        The role grants two sets of permissions:

        1. **DynamoDB** — full CRUD operations on the runs table and its
           indexes.  Scoped to the specific table ARN to follow least-privilege.
        2. **Secrets Manager** — ``GetSecretValue`` on secrets under the
           ``dispatch/*`` prefix.  This allows the application to read secrets
           at runtime without baking them into the container image.

        Args:
            table: The DynamoDB table construct (for ARN reference).
            aws_region: The AWS region for Secrets Manager ARN construction.

        Returns:
            The IAM ``Role`` construct.
        """
        role = iam.Role(
            self,
            "AppRunnerInstanceRole",
            assumed_by=iam.ServicePrincipal("tasks.apprunner.amazonaws.com"),
            description=(
                "Instance role for the Copilot Dispatch AppRunner service, granting "
                "access to DynamoDB and Secrets Manager."
            ),
        )

        # DynamoDB — CRUD access scoped to the runs table and its indexes.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="DynamoDBAccess",
                effect=iam.Effect.ALLOW,
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                    "dynamodb:DeleteItem",
                ],
                resources=[
                    table.table_arn,
                    f"{table.table_arn}/index/*",
                ],
            )
        )

        # Secrets Manager — read-only access to dispatch secrets.
        # The wildcard after "dispatch/" covers the random suffix that
        # Secrets Manager appends to secret ARNs.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="SecretsManagerAccess",
                effect=iam.Effect.ALLOW,
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    (
                        f"arn:aws:secretsmanager:{aws_region}:"
                        f"{self.account}:secret:dispatch/*"
                    ),
                ],
            )
        )

        return role

    def _create_ecr_access_role(self) -> iam.Role:
        """Create the IAM role authorising AppRunner to pull images from ECR.

        AppRunner's build service assumes this role during image pull.  The
        attached AWS managed policy provides the necessary ECR read permissions.

        Returns:
            The IAM ``Role`` construct.
        """
        role = iam.Role(
            self,
            "AppRunnerEcrAccessRole",
            assumed_by=iam.ServicePrincipal("build.apprunner.amazonaws.com"),
            description=(
                "Access role allowing AppRunner to pull container images "
                "from the Copilot Dispatch ECR repository."
            ),
        )
        role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AWSAppRunnerServicePolicyForECRAccess"
            )
        )
        return role

    def _create_auto_scaling_configuration(
        self,
        min_size: int,
        max_size: int,
    ) -> apprunner.CfnAutoScalingConfiguration:
        """Create the AppRunner auto-scaling configuration.

        ``min_size=1`` keeps one instance warm at all times.  AppRunner does
        not support ``min_size=0`` (scale-to-zero) in the current API — the
        minimum allowed value is 1.

        ``max_concurrency=100`` controls how many concurrent requests a single
        instance handles before AppRunner provisions another instance.

        Args:
            min_size: Minimum number of instances (must be >= 1).
            max_size: Maximum number of instances.

        Returns:
            The ``CfnAutoScalingConfiguration`` construct.
        """
        return apprunner.CfnAutoScalingConfiguration(
            self,
            "AutoScalingConfig",
            auto_scaling_configuration_name="dispatch-scaling",
            max_concurrency=100,
            max_size=max_size,
            min_size=min_size,
        )

    def _create_apprunner_service(
        self,
        *,
        repository: ecr.Repository,
        ecr_access_role: iam.Role,
        instance_role: iam.Role,
        auto_scaling: apprunner.CfnAutoScalingConfiguration,
        api_key_secret: secretsmanager.ISecret,
        github_pat_secret: secretsmanager.ISecret,
        webhook_secret: secretsmanager.ISecret,
        openai_key_secret: secretsmanager.ISecret,
        table_name: str,
        aws_region: str,
        github_owner: str,
        github_repo: str,
        cpu: str,
        memory: str,
        service_url: str | None = None,
    ) -> apprunner.CfnService:
        """Create the AppRunner service running the Copilot Dispatch API.

        Uses the L1 ``CfnService`` construct because the L2 alpha module does
        not support all required features (Secrets Manager injection via the
        ``secrets`` field in ``ImageConfiguration``, auto-scaling to zero).

        The service is configured to:

        - Pull from the ECR repository with auto-deployment on image push.
        - Inject Secrets Manager values as environment variables at runtime
          (not baked into the CloudFormation template as plaintext).
        - Expose port 8000 with HTTP health checks on ``/health``.
        - Use the specified CPU, memory, and auto-scaling configuration.

        Args:
            repository: The ECR repository construct.
            ecr_access_role: IAM role for ECR image pull.
            instance_role: IAM role assumed by the running container.
            auto_scaling: The auto-scaling configuration resource.
            api_key_secret: Secrets Manager reference for the API key.
            github_pat_secret: Secrets Manager reference for the GitHub PAT.
            webhook_secret: Secrets Manager reference for the webhook secret.
            table_name: DynamoDB table name to pass as an env var.
            aws_region: AWS region to pass as an env var.
            cpu: CPU allocation string (e.g. ``"1024"`` for 1 vCPU).
            memory: Memory allocation string (e.g. ``"2048"`` for 2 GB).
            service_url: Optional externally reachable HTTPS base URL. When
                set, injected as ``DISPATCH_SERVICE_URL`` so the API can
                construct correct callback URLs behind the TLS-terminating
                App Runner load balancer.

        Returns:
            The AppRunner ``CfnService`` construct.
        """
        env_vars = [
            apprunner.CfnService.KeyValuePairProperty(
                name="DISPATCH_APP_ENV",
                value="production",
            ),
            apprunner.CfnService.KeyValuePairProperty(
                name="AWS_REGION", value=aws_region
            ),
            apprunner.CfnService.KeyValuePairProperty(
                name="DISPATCH_DYNAMODB_TABLE_NAME",
                value=table_name,
            ),
            apprunner.CfnService.KeyValuePairProperty(
                name="DISPATCH_GITHUB_OWNER",
                value=github_owner,
            ),
            apprunner.CfnService.KeyValuePairProperty(
                name="DISPATCH_GITHUB_REPO",
                value=github_repo,
            ),
            apprunner.CfnService.KeyValuePairProperty(
                name="DISPATCH_GITHUB_WORKFLOW_ID",
                value="agent-executor.yml",
            ),
            apprunner.CfnService.KeyValuePairProperty(
                name="DISPATCH_LOG_LEVEL",
                value="INFO",
            ),
        ]

        if service_url:
            env_vars.append(
                apprunner.CfnService.KeyValuePairProperty(
                    name="DISPATCH_SERVICE_URL",
                    value=service_url,
                )
            )

        return apprunner.CfnService(
            self,
            "AppRunnerService",
            service_name="dispatch-api",
            source_configuration=apprunner.CfnService.SourceConfigurationProperty(
                authentication_configuration=apprunner.CfnService.AuthenticationConfigurationProperty(
                    access_role_arn=ecr_access_role.role_arn,
                ),
                auto_deployments_enabled=True,
                image_repository=apprunner.CfnService.ImageRepositoryProperty(
                    image_identifier=f"{repository.repository_uri}:latest",
                    image_repository_type="ECR",
                    image_configuration=apprunner.CfnService.ImageConfigurationProperty(
                        port="8000",
                        # Plain-text environment variables — non-sensitive
                        # configuration values that are safe to include in the
                        # CloudFormation template.
                        runtime_environment_variables=env_vars,
                        # Secrets sourced from Secrets Manager at runtime.
                        # AppRunner resolves these ARNs to their secret values
                        # and injects them as environment variables — the
                        # plaintext values never appear in the CloudFormation
                        # template.
                        runtime_environment_secrets=[
                            apprunner.CfnService.KeyValuePairProperty(
                                name="DISPATCH_API_KEY",
                                value=api_key_secret.secret_arn,
                            ),
                            apprunner.CfnService.KeyValuePairProperty(
                                name="DISPATCH_WEBHOOK_SECRET",
                                value=webhook_secret.secret_arn,
                            ),
                            apprunner.CfnService.KeyValuePairProperty(
                                name="GITHUB_PAT",
                                value=github_pat_secret.secret_arn,
                            ),
                            apprunner.CfnService.KeyValuePairProperty(
                                name="OPENAI_API_KEY",
                                value=openai_key_secret.secret_arn,
                            ),
                        ],
                    ),
                ),
            ),
            instance_configuration=apprunner.CfnService.InstanceConfigurationProperty(
                cpu=cpu,
                memory=memory,
                instance_role_arn=instance_role.role_arn,
            ),
            health_check_configuration=apprunner.CfnService.HealthCheckConfigurationProperty(
                protocol="HTTP",
                path="/health",
                interval=10,
                timeout=5,
                healthy_threshold=1,
                unhealthy_threshold=5,
            ),
            auto_scaling_configuration_arn=auto_scaling.attr_auto_scaling_configuration_arn,
        )
