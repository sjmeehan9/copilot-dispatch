from aws_cdk import App, Stack
from aws_cdk import aws_secretsmanager as sm

app = App()
stack = Stack(app, "test")
secret = sm.Secret.from_secret_name_v2(stack, "sec", "dispatch/webhook-secret")
print("secret_arn:", secret.secret_arn)
try:
    print("secret_full_arn:", secret.secret_full_arn)
except Exception as e:
    print("error:", e)
