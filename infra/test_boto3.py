import boto3

client = boto3.client("secretsmanager", region_name="ap-southeast-2")
print(client.describe_secret(SecretId="dispatch/api-key")["ARN"])
