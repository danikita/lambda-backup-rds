import os
import json
import boto3
import subprocess
import datetime
from zoneinfo import ZoneInfo

s3_client = boto3.client("s3")
secrets_client = boto3.client("secretsmanager")


def get_db_credentials_from_secret(secret_arn: str):
    """Retrieves username and password from Secrets Manager."""
    response = secrets_client.get_secret_value(SecretId=secret_arn)
    secret = json.loads(response["SecretString"])
    return secret["username"], secret["password"]


def list_databases(db_host: str, db_port: str, db_user: str, db_password: str):
    """Lists the databases (excluding system schemas)."""
    cmd = [
        "mysql",
        "-h", db_host,
        "-P", db_port,
        "-u", db_user,
        f"-p{db_password}",
        "-N",
        "-e", "SHOW DATABASES;"
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"mysql failed: {result.stderr}")

    dbs = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    exclude = {"information_schema", "performance_schema", "mysql", "sys"}
    return [db for db in dbs if db not in exclude]


def lambda_handler(event, context):
    """
    Accepted parameters in the test event (all optional — fallback to environment variables):

    {
        "db_host":      "my-rds.amazonaws.com",          // RDS endpoint
        "db_port":      "3306",                          // Port (default: 3306)
        "secret_arn":   "arn:aws:secretsmanager:...",    // Secrets Manager ARN
        "db_user":      "admin",                         // Direct username (alternative to secret_arn)
        "db_password":  "password",                      // Direct password (alternative to secret_arn)
        "s3_bucket":    "my-bucket",                     // S3 bucket name
        "s3_prefix":    "mysql/"                         // S3 prefix (default: "mysql/")
    }

    Credential priority: secret_arn > db_user + db_password > environment variables.
    """

    # ── Configurações de conexão ────────────────────────────────────────────────
    db_host = (
        event.get("db_host")
        or os.getenv("DB_HOST", "")
    )
    db_port = str(
        event.get("db_port")
        or os.getenv("DB_PORT", "3306")
    )
    s3_bucket = (
        event.get("s3_bucket")
        or os.getenv("S3_BUCKET", "")
    )
    s3_prefix = (
        event.get("s3_prefix")
        or os.getenv("S3_PREFIX", "mysql/")
    )

    # Ensures that the prefix ends with "/"
    if s3_prefix and not s3_prefix.endswith("/"):
        s3_prefix += "/"

    # ── Credentials ─────────────────────────────────────────────────────────────
    secret_arn = event.get("secret_arn") or os.getenv("SECRET_ARN", "")

    if secret_arn:
        try:
            db_user, db_password = get_db_credentials_from_secret(secret_arn)
        except Exception as e:
            return {
                "statusCode": 500,
                "body": f"Failed to retrieve secret '{secret_arn}': {str(e)}"
            }
    elif event.get("db_user") and event.get("db_password"):
        db_user = event["db_user"]
        db_password = event["db_password"]
    elif os.getenv("DB_USER") and os.getenv("DB_PASSWORD"):
        db_user = os.getenv("DB_USER")
        db_password = os.getenv("DB_PASSWORD")
    else:
        return {
            "statusCode": 400,
            "body": (
                "Credentials not provided. "
                "Inform either 'secret_arn' or 'db_user'+'db_password' in the event, "
                "or define the environment variables SECRET_ARN or DB_USER + DB_PASSWORD."
            )
        }

    # ── Validações básicas ──────────────────────────────────────────────────────
    if not db_host:
        return {"statusCode": 400, "body": " 'db_host' not provided."}
    if not s3_bucket:
        return {"statusCode": 400, "body": " 's3_bucket' not provided."}

    # ── Listar bancos ───────────────────────────────────────────────────────────
    try:
        databases = list_databases(db_host, db_port, db_user, db_password)
    except Exception as e:
        return {
            "statusCode": 500,
            "body": f"❌ Failed to list databases: {str(e)}"
        }

    if not databases:
        return {
            "statusCode": 200,
            "body": "No databases found (excluding system schemas)."
        }

    # ── Dump + upload ───────────────────────────────────────────────────────────
    timestamp = datetime.datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%Y%m%d%H%M%S")
    results = []

    for db in databases:
        filename = f"{db}_{timestamp}.sql"
        filepath = f"/tmp/{filename}"

        dump_cmd = [
            "mysqldump",
            "-h", db_host,
            "-P", db_port,
            "-u", db_user,
            f"-p{db_password}",
            "--databases", db,
            "-r", filepath
        ]

        result = subprocess.run(dump_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            results.append(f" {db} — mysqldump failed: {result.stderr.strip()}")
            continue

        try:
            s3_key = f"{s3_prefix}{filename}"
            s3_client.upload_file(filepath, s3_bucket, s3_key)
            results.append(f"✅ {db} → s3://{s3_bucket}/{s3_key}")
        except Exception as e:
            results.append(f"❌ {db} — upload failed: {str(e)}")

    return {
        "statusCode": 200,
        "body": results
    }
