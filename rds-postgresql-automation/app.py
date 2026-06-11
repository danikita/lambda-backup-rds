import os
import json
import boto3
import subprocess
import datetime
from zoneinfo import ZoneInfo

s3_client = boto3.client("s3")
secrets_client = boto3.client("secretsmanager")


def get_db_credentials(secret_arn):
    """Retrieves the username and password from AWS Secrets Manager."""
    response = secrets_client.get_secret_value(SecretId=secret_arn)
    secret = json.loads(response["SecretString"])
    return secret["username"], secret["password"]


def list_databases(db_host, db_port, db_user, db_password):
    """Lists all PostgreSQL databases, excluding template databases and the rdsadmin database."""
    env = os.environ.copy()
    env["PGPASSWORD"] = db_password

    cmd = [
        "psql",
        "-h", db_host,
        "-p", str(db_port),
        "-U", db_user,
        "-d", "postgres",
        "-t",  # sem headers
        "-c", """
        SELECT datname
        FROM pg_database
        WHERE datistemplate = false
          AND datname NOT IN ('rdsadmin');
        """
    ]

    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"psql failed to list databases: {result.stderr}")

    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def dump_database(db_host, db_port, db_user, db_password, db_name, filepath):
    """Runs pg_dump for a specific database and saves it to a local file."""
    env = os.environ.copy()
    env["PGPASSWORD"] = db_password

    cmd = [
        "pg_dump",
        "-h", db_host,
        "-p", str(db_port),
        "-U", db_user,
        "-d", db_name,
        "-F", "c",   # custom format (compressed, restorable with pg_restore)
        "-f", filepath
    ]

    subprocess.check_call(cmd, env=env)


def lambda_handler(event, context):
    """
    Expected event parameters (all required, except either secret_arn OR username+password):

    {
        "db_host":    "my-rds.xxx.rds.amazonaws.com",   # RDS endpoint 
        "db_port":    5432,                               # port (default: 5432)
        "s3_bucket":  "my-bucket-name",               # bucket S3 name
        "s3_prefix":  "postgres/",                       # prefix/folder S3

        // Option 1 — credentials via AWS Secrets Manager (recommended):
        "secret_arn": "arn:aws:secretsmanager:...",
        
        // Option 2 — direct credentials (not recommended for production):
        "username": "my_username",
        "password": "my_password"
    }
    """

    # ── Connection parameters ──────────────────────────────────────────────────
    db_host = event.get("db_host")
    db_port = event.get("db_port", 5432)
    s3_bucket = event.get("s3_bucket")
    s3_prefix = event.get("s3_prefix", "postgres/")

    if not db_host:
        return {"statusCode": 400, "body": "Missing required parameter: db_host"}
    if not s3_bucket:
        return {"statusCode": 400, "body": "Missing required parameter: s3_bucket"}

    # ── Credentials ────────────────────────────────────────────────────────────
    secret_arn = event.get("secret_arn")

    if secret_arn:
        try:
            db_user, db_password = get_db_credentials(secret_arn)
        except Exception as e:
            return {"statusCode": 500, "body": f"Failed to retrieve credentials from AWS Secrets Manager: {str(e)}"}
    else:
        db_user = event.get("username")
        db_password = event.get("password")
        if not db_user or not db_password:
            return {
                "statusCode": 400,
                "body": "Provide 'secret_arn' or both 'username' and 'password' in the event."
            }

    # ── Timestamp (Brasília) ───────────────────────────────────────
    timestamp = datetime.datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%Y%m%d%H%M%S")

    # ── Database listing ────────────────────────────────────────────────────
    try:
        databases = list_databases(db_host, db_port, db_user, db_password)
    except Exception as e:
        return {"statusCode": 500, "body": f"Failed to list databases: {str(e)}"}

    if not databases:
        return {"statusCode": 200, "body": "No databases found for backup."}

    # ── Dump + upload to S3 ─────────────────────────────────────────────────
    results = []

    for db_name in databases:
        filename = f"{db_name}_{timestamp}.dump"
        filepath = f"/tmp/{filename}"
        s3_key = f"{s3_prefix}{filename}"

        try:
            dump_database(db_host, db_port, db_user, db_password, db_name, filepath)
            s3_client.upload_file(filepath, s3_bucket, s3_key)
            results.append(f"{db_name} → s3://{s3_bucket}/{s3_key}")
        except subprocess.CalledProcessError as e:
            results.append(f"{db_name} — pg_dump failed: {e}")
        except Exception as e:
            results.append(f"{db_name} — erro: {str(e)}")
        finally:
            # Removes the temporary file to prevent the Lambda /tmp directory from running out of space.
            if os.path.exists(filepath):
                os.remove(filepath)

    return {
        "statusCode": 200,
        "body": results
    }
