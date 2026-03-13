"""DynamoDB Local initialization script.

Creates the `dispatch-runs` table in DynamoDB Local for development.
"""

import boto3
from botocore.exceptions import ClientError


def init_table() -> None:
    """Create the dispatch-runs table in DynamoDB Local."""
    dynamodb = boto3.resource(
        "dynamodb",
        endpoint_url="http://localhost:8100",
        region_name="ap-southeast-2",
        aws_access_key_id="dummy",
        aws_secret_access_key="dummy",
    )

    table_name = "dispatch-runs"

    try:
        print(f"Creating table {table_name}...")
        table = dynamodb.create_table(
            TableName=table_name,
            KeySchema=[{"AttributeName": "run_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "run_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        print(f"Table {table_name} created successfully.")

        # Attempt to enable TTL
        client = boto3.client(
            "dynamodb",
            endpoint_url="http://localhost:8100",
            region_name="ap-southeast-2",
            aws_access_key_id="dummy",
            aws_secret_access_key="dummy",
        )
        try:
            client.update_time_to_live(
                TableName=table_name,
                TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"},
            )
            print("TTL enabled on 'ttl' attribute.")
        except ClientError as e:
            print(f"Could not enable TTL (expected in some local versions): {e}")

    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceInUseException":
            print(f"Table {table_name} already exists.")
        else:
            raise


if __name__ == "__main__":
    init_table()
