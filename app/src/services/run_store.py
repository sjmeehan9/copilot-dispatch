"""DynamoDB run store service.

Provides the RunStore class for managing agent run records in DynamoDB.
Handles creation, retrieval, status updates, result/error storage, and
stale run detection.
"""

import datetime
import logging
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

from app.src.api.models import AgentRunRequest
from app.src.config import get_settings
from app.src.exceptions import ConflictError, ExternalServiceError, RunNotFoundError

logger = logging.getLogger(__name__)


def _decimal_to_native(obj: Any) -> Any:
    """Recursively convert Decimal objects to int or float."""
    if isinstance(obj, list):
        return [_decimal_to_native(i) for i in obj]
    elif isinstance(obj, dict):
        return {k: _decimal_to_native(v) for k, v in obj.items()}
    elif isinstance(obj, Decimal):
        if obj % 1 == 0:
            return int(obj)
        return float(obj)
    return obj


class RunStore:
    """Service for managing agent run records in DynamoDB."""

    def __init__(self, dynamodb_resource: Any, table_name: str) -> None:
        """Initialize the RunStore.

        Args:
            dynamodb_resource: A boto3 DynamoDB resource.
            table_name: The name of the DynamoDB table.
        """
        self._dynamodb = dynamodb_resource
        self._table = self._dynamodb.Table(table_name)

    def create_run(self, run_id: str, request: AgentRunRequest) -> dict[str, Any]:
        """Create a new run record in DynamoDB.

        Args:
            run_id: The unique identifier for the run.
            request: The validated agent run request.

        Returns:
            The created run record as a dictionary.

        Raises:
            ConflictError: If a run with the given ID already exists.
            ExternalServiceError: If a DynamoDB error occurs.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        created_at = now.isoformat()
        ttl = int((now + datetime.timedelta(days=90)).timestamp())

        settings = get_settings()
        callback_url = request.callback_url or settings.default_callback_url

        item: dict[str, Any] = {
            "run_id": run_id,
            "repository": request.repository,
            "branch": request.branch,
            "role": request.role,
            "status": "dispatched",
            "model": request.model,
            "agent_instructions": request.agent_instructions,
            "timeout_minutes": request.timeout_minutes,
            "created_at": created_at,
            "updated_at": created_at,
            "result": None,
            "error": None,
            "ttl": ttl,
        }

        if request.system_instructions is not None:
            item["system_instructions"] = request.system_instructions
        if request.pr_number is not None:
            item["pr_number"] = request.pr_number
        if callback_url is not None:
            item["callback_url"] = str(callback_url)
        if request.skill_paths is not None:
            item["skill_paths"] = request.skill_paths
        if request.agent_paths is not None:
            item["agent_paths"] = request.agent_paths

        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(run_id)",
            )
            logger.info(
                "Created run record",
                extra={
                    "run_id": run_id,
                    "repository": request.repository,
                    "role": request.role,
                },
            )
            return item
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ConflictError(error_message=f"Run {run_id} already exists")
            raise ExternalServiceError(
                error_message="Failed to create run record in DynamoDB",
                error_details={"dynamodb_error": str(e)},
            )

    def get_run(self, run_id: str) -> dict[str, Any]:
        """Retrieve a run record by ID, with stale run detection.

        Args:
            run_id: The unique identifier for the run.

        Returns:
            The run record as a dictionary.

        Raises:
            RunNotFoundError: If the run does not exist.
            ExternalServiceError: If a DynamoDB error occurs.
        """
        try:
            response = self._table.get_item(Key={"run_id": run_id})
        except ClientError as e:
            raise ExternalServiceError(
                error_message="Failed to retrieve run record from DynamoDB",
                error_details={"dynamodb_error": str(e)},
            )

        item = response.get("Item")
        if not item:
            raise RunNotFoundError(error_message=f"Run {run_id} not found")

        item = _decimal_to_native(item)
        logger.debug("Retrieved run record", extra={"run_id": run_id})

        return self._check_stale_run(item)

    def update_run_status(self, run_id: str, status: str) -> dict[str, Any]:
        """Update the status of a run record.

        Args:
            run_id: The unique identifier for the run.
            status: The new status.

        Returns:
            The updated run record as a dictionary.

        Raises:
            RunNotFoundError: If the run does not exist.
            ExternalServiceError: If a DynamoDB error occurs.
        """
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            response = self._table.update_item(
                Key={"run_id": run_id},
                UpdateExpression="SET #status = :status, updated_at = :updated_at",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":status": status,
                    ":updated_at": now,
                },
                ConditionExpression="attribute_exists(run_id)",
                ReturnValues="ALL_NEW",
            )
            item = _decimal_to_native(response["Attributes"])
            logger.info(
                "Updated run status",
                extra={"run_id": run_id, "status": status},
            )
            return item
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise RunNotFoundError(error_message=f"Run {run_id} not found")
            raise ExternalServiceError(
                error_message="Failed to update run status in DynamoDB",
                error_details={"dynamodb_error": str(e)},
            )

    def update_run_result(
        self,
        run_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update a run record with a result or error payload.

        Args:
            run_id: The unique identifier for the run.
            status: The new status.
            result: Optional result payload.
            error: Optional error payload.

        Returns:
            The updated run record as a dictionary.

        Raises:
            ConflictError: If the run is already in a terminal state.
            ExternalServiceError: If a DynamoDB error occurs.
        """
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()

        update_expr = "SET #status = :status, updated_at = :updated_at"
        expr_names = {"#status": "status"}
        expr_values: dict[str, Any] = {
            ":status": status,
            ":updated_at": now,
            ":success": "success",
            ":failure": "failure",
            ":timeout": "timeout",
        }

        if result is not None:
            update_expr += ", #result = :result"
            expr_names["#result"] = "result"
            expr_values[":result"] = result

        if error is not None:
            update_expr += ", #error = :error"
            expr_names["#error"] = "error"
            expr_values[":error"] = error

        try:
            response = self._table.update_item(
                Key={"run_id": run_id},
                UpdateExpression=update_expr,
                ExpressionAttributeNames=expr_names,
                ExpressionAttributeValues=expr_values,
                ConditionExpression="attribute_exists(run_id) AND NOT #status IN (:success, :failure, :timeout)",
                ReturnValues="ALL_NEW",
            )
            item = _decimal_to_native(response["Attributes"])
            logger.info(
                "Updated run result",
                extra={"run_id": run_id, "status": status},
            )
            return item
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ConflictError(
                    error_message=f"Run {run_id} is already in terminal state or does not exist"
                )
            raise ExternalServiceError(
                error_message="Failed to update run result in DynamoDB",
                error_details={"dynamodb_error": str(e)},
            )

    def update_run_error(
        self,
        run_id: str,
        error_code: str,
        error_message: str,
        error_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update a run record with an error payload.

        Args:
            run_id: The unique identifier for the run.
            error_code: The error code.
            error_message: The error message.
            error_details: Optional error details.

        Returns:
            The updated run record as a dictionary.
        """
        error_payload: dict[str, Any] = {
            "error_code": error_code,
            "error_message": error_message,
        }
        if error_details is not None:
            error_payload["error_details"] = error_details

        logger.warning(
            "Updating run with error",
            extra={"run_id": run_id, "error_code": error_code},
        )
        return self.update_run_result(
            run_id=run_id,
            status="failure",
            result=None,
            error=error_payload,
        )

    def _check_stale_run(self, item: dict[str, Any]) -> dict[str, Any]:
        """Check if a run is stale and update it to timeout if necessary.

        Args:
            item: The run record to check.

        Returns:
            The updated run record if it was stale, otherwise the original record.
        """
        status = item.get("status")
        if status not in ("dispatched", "running"):
            return item

        created_at_str = item.get("created_at")
        timeout_minutes = item.get("timeout_minutes", 30)
        # Add a 10-minute buffer beyond the agent timeout to allow for
        # workflow setup, pre/post-processing, and result posting.
        stale_buffer_minutes = 10
        stale_threshold_minutes = timeout_minutes + stale_buffer_minutes

        if not created_at_str:
            return item

        try:
            created_at = datetime.datetime.fromisoformat(created_at_str)
        except ValueError:
            return item

        now = datetime.datetime.now(datetime.timezone.utc)
        if now < created_at + datetime.timedelta(minutes=stale_threshold_minutes):
            return item

        # Run is stale, attempt conditional update
        run_id = item["run_id"]
        new_status = "timeout"
        error_payload = {
            "error_code": "STALE_RUN_TIMEOUT",
            "error_message": f"Run exceeded stale detection threshold of {stale_threshold_minutes} minutes (agent timeout: {timeout_minutes}m + buffer: {stale_buffer_minutes}m) without reporting a result. The workflow may have crashed or been cancelled.",
            "error_details": {
                "timeout_minutes": timeout_minutes,
                "detected_at": now.isoformat(),
            },
        }

        try:
            response = self._table.update_item(
                Key={"run_id": run_id},
                UpdateExpression="SET #status = :new_status, updated_at = :updated_at, #error = :error",
                ExpressionAttributeNames={
                    "#status": "status",
                    "#error": "error",
                },
                ExpressionAttributeValues={
                    ":new_status": new_status,
                    ":updated_at": now.isoformat(),
                    ":error": error_payload,
                    ":dispatched": "dispatched",
                    ":running": "running",
                },
                ConditionExpression="#status IN (:dispatched, :running)",
                ReturnValues="ALL_NEW",
            )
            updated_item = _decimal_to_native(response["Attributes"])
            logger.warning(
                "Detected and updated stale run",
                extra={"run_id": run_id, "timeout_minutes": timeout_minutes},
            )
            return updated_item
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Another process already updated it, re-fetch
                try:
                    response = self._table.get_item(Key={"run_id": run_id})
                    if "Item" in response:
                        return _decimal_to_native(response["Item"])
                except ClientError:
                    pass
            # If re-fetch fails or other error, just return the original item
            return item


def get_run_store() -> RunStore:
    """Factory function to create a RunStore instance.

    Returns:
        A configured RunStore instance.
    """
    settings = get_settings()

    if settings.dynamodb_endpoint_url:
        dynamodb = boto3.resource(
            "dynamodb",
            endpoint_url=settings.dynamodb_endpoint_url,
            region_name="us-east-1",
            aws_access_key_id="dummy",
            aws_secret_access_key="dummy",
        )
    else:
        dynamodb = boto3.resource("dynamodb")

    return RunStore(dynamodb, settings.dynamodb_table_name)
