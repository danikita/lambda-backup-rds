"""
Lambda: lambda-sqlserver-ec2-backup

Instead of connecting directly to SQL Server (as in the RDS scenario),
this Lambda triggers an SSM command (AWS-RunPowerShellScript) on the EC2
instance that runs SQL Server. The command executes the
'backup_to_s3.ps1' script, which performs the local BACKUP DATABASE and
uploads the file to S3.

Advantages of this approach:
  - No ODBC driver / pyodbc / Docker required.
  - No need to open port 1433 to the Lambda.
  - The Lambda only needs ssm:SendCommand /
    ssm:GetCommandInvocation permission on the instance.
  - SQL Server performs the backup locally and the AWS CLI (or the EC2
    IAM Role) uploads the file to S3 -- nothing passes through the Lambda.

Example payload (event):
{
  "INSTANCE_ID": "i-0123456789abcdef0",
  "BACKUP_TYPE": "FULL",
  "S3_BUCKET": "my-backups-bucket",
  "S3_PREFIX": "backups/full/",
  "SQL_INSTANCE": "localhost",
  "LOCAL_DIR": "C:\\SQLBackups",
  "RETAIN_LOCAL": "false",
  "WAIT_FOR_COMPLETION": true
}
"""

import json
import time
import boto3

VALID_BACKUP_TYPES = {"FULL", "DIFFERENTIAL"}

# Document used to run the command via SSM. Must match backup_to_s3.ps1
# (kept here so the call doesn't depend on external files in the SSM
# document. Alternatively, use a custom SSM document that already
# contains this script and reference it by name).
DEFAULT_DOCUMENT_NAME = "AWS-RunPowerShellScript"

POLL_INTERVAL_SEC = 10
POLL_TIMEOUT_SEC = 1800  # 30 minutes (large backups may take a while)


def build_command(event):
    """Builds the PowerShell command that will be executed on the EC2 instance."""

    backup_type = event.get("BACKUP_TYPE", "FULL").upper()
    if backup_type not in VALID_BACKUP_TYPES:
        raise ValueError(
            f"Invalid BACKUP_TYPE '{backup_type}'. Use FULL or DIFFERENTIAL."
        )

    s3_bucket = event.get("S3_BUCKET")
    if not s3_bucket:
        raise ValueError("S3_BUCKET is required in the event.")

    s3_prefix = event.get("S3_PREFIX", "")
    sql_instance = event.get("SQL_INSTANCE", "localhost")
    local_dir = event.get("LOCAL_DIR", r"C:\SQLBackups")
    retain_local = str(event.get("RETAIN_LOCAL", "false")).lower()

    # The backup_to_s3.ps1 script must already be copied to the EC2 instance
    # (e.g. C:\Scripts\backup_to_s3.ps1) during provisioning (user-data,
    # custom AMI, or configuration pipeline).
    script_path = event.get("SCRIPT_PATH", r"C:\Scripts\backup_to_s3.ps1")

    # Set the process environment variables before calling the script
    command = (
        f'$env:BACKUP_TYPE="{backup_type}"; '
        f'$env:S3_BUCKET="{s3_bucket}"; '
        f'$env:S3_PREFIX="{s3_prefix}"; '
        f'$env:SQL_INSTANCE="{sql_instance}"; '
        f'$env:LOCAL_DIR="{local_dir}"; '
        f'$env:RETAIN_LOCAL="{retain_local}"; '
        f'& "{script_path}"'
    )

    return command


def lambda_handler(event, context):
    ssm = boto3.client("ssm")

    instance_id = event.get("INSTANCE_ID")
    if not instance_id:
        return {
            "statusCode": 400,
            "body": ["Error: INSTANCE_ID is required in the event."],
        }

    wait = event.get("WAIT_FOR_COMPLETION", True)

    try:
        command = build_command(event)
    except ValueError as e:
        return {"statusCode": 400, "body": [f"Error: {str(e)}"]}

    try:
        response = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName=DEFAULT_DOCUMENT_NAME,
            Parameters={"commands": [command]},
            TimeoutSeconds=3600,
            Comment=f"SQL Server backup ({event.get('BACKUP_TYPE', 'FULL')}) to S3",
        )
    except Exception as e:
        return {"statusCode": 500, "body": [f"Error sending SSM command: {str(e)}"]}

    command_id = response["Command"]["CommandId"]

    if not wait:
        return {
            "statusCode": 200,
            "body": [
                f"[STARTED] command_id={command_id} instance={instance_id} "
                f"(asynchronous execution, not awaited)."
            ],
        }

    # Wait for the command to finish, polling its status
    deadline = time.time() + POLL_TIMEOUT_SEC
    status = "Pending"

    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_SEC)

        try:
            invocation = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
            )
        except ssm.exceptions.InvocationDoesNotExist:
            # Not yet registered, try again
            continue

        status = invocation["Status"]
        print(f"[poll] command_id={command_id} status={status}")

        if status in ("Success", "Failed", "Cancelled", "TimedOut"):
            stdout = invocation.get("StandardOutputContent", "").strip()
            stderr = invocation.get("StandardErrorContent", "").strip()

            body_lines = stdout.splitlines() if stdout else []
            if status != "Success":
                body_lines.append(f"[STATUS] {status}")
                if stderr:
                    body_lines.append(f"[STDERR] {stderr}")

            status_code = 200 if status == "Success" else 500
            return {"statusCode": status_code, "body": body_lines or [f"[STATUS] {status}"]}

    return {
        "statusCode": 500,
        "body": [f"Timeout waiting for command_id={command_id} to finish (status={status})."],
    }
