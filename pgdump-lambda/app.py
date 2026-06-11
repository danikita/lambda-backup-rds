import os
import json
import boto3
import subprocess
import datetime
from zoneinfo import ZoneInfo

s3_client = boto3.client("s3")
secrets_client = boto3.client("secretsmanager")


def get_db_credentials(secret_arn):
    """Busca usuário e senha no Secrets Manager."""
    response = secrets_client.get_secret_value(SecretId=secret_arn)
    secret = json.loads(response["SecretString"])
    return secret["username"], secret["password"]


def list_databases(db_host, db_port, db_user, db_password):
    """Lista todos os bancos de dados do PostgreSQL, excluindo templates e rdsadmin."""
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
        raise Exception(f"psql falhou ao listar bancos: {result.stderr}")

    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def dump_database(db_host, db_port, db_user, db_password, db_name, filepath):
    """Executa o pg_dump de um banco específico para um arquivo local."""
    env = os.environ.copy()
    env["PGPASSWORD"] = db_password

    cmd = [
        "pg_dump",
        "-h", db_host,
        "-p", str(db_port),
        "-U", db_user,
        "-d", db_name,
        "-F", "c",   # formato custom (comprimido, restaurável com pg_restore)
        "-f", filepath
    ]

    subprocess.check_call(cmd, env=env)


def lambda_handler(event, context):
    """
    Parâmetros esperados no event (todos obrigatórios, exceto secret_arn OU username+password):

    {
        "db_host":    "meu-rds.xxx.rds.amazonaws.com",   # endpoint do RDS
        "db_port":    5432,                               # porta (default: 5432)
        "s3_bucket":  "meu-bucket-backup",               # nome do bucket S3
        "s3_prefix":  "postgres/",                       # prefixo/pasta no S3

        // Opção 1 — credenciais via Secrets Manager (recomendado):
        "secret_arn": "arn:aws:secretsmanager:...",

        // Opção 2 — credenciais diretas (não recomendado em produção):
        "username": "meu_usuario",
        "password": "minha_senha"
    }
    """

    # ── Parâmetros de conexão ──────────────────────────────────────────────────
    db_host = event.get("db_host")
    db_port = event.get("db_port", 5432)
    s3_bucket = event.get("s3_bucket")
    s3_prefix = event.get("s3_prefix", "postgres/")

    if not db_host:
        return {"statusCode": 400, "body": "❌ Parâmetro obrigatório ausente: db_host"}
    if not s3_bucket:
        return {"statusCode": 400, "body": "❌ Parâmetro obrigatório ausente: s3_bucket"}

    # ── Credenciais ────────────────────────────────────────────────────────────
    secret_arn = event.get("secret_arn")

    if secret_arn:
        try:
            db_user, db_password = get_db_credentials(secret_arn)
        except Exception as e:
            return {"statusCode": 500, "body": f"❌ Falha ao buscar credenciais no Secrets Manager: {str(e)}"}
    else:
        db_user = event.get("username")
        db_password = event.get("password")
        if not db_user or not db_password:
            return {
                "statusCode": 400,
                "body": "❌ Forneça 'secret_arn' ou ambos 'username' e 'password' no event."
            }

    # ── Timestamp (horário de Brasília) ───────────────────────────────────────
    timestamp = datetime.datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%Y%m%d%H%M%S")

    # ── Listagem de bancos ────────────────────────────────────────────────────
    try:
        databases = list_databases(db_host, db_port, db_user, db_password)
    except Exception as e:
        return {"statusCode": 500, "body": f"❌ Falha ao listar bancos: {str(e)}"}

    if not databases:
        return {"statusCode": 200, "body": "⚠️ Nenhum banco encontrado para backup."}

    # ── Dump + upload para S3 ─────────────────────────────────────────────────
    results = []

    for db_name in databases:
        filename = f"{db_name}_{timestamp}.dump"
        filepath = f"/tmp/{filename}"
        s3_key = f"{s3_prefix}{filename}"

        try:
            dump_database(db_host, db_port, db_user, db_password, db_name, filepath)
            s3_client.upload_file(filepath, s3_bucket, s3_key)
            results.append(f"✅ {db_name} → s3://{s3_bucket}/{s3_key}")
        except subprocess.CalledProcessError as e:
            results.append(f"❌ {db_name} — pg_dump falhou: {e}")
        except Exception as e:
            results.append(f"❌ {db_name} — erro: {str(e)}")
        finally:
            # Remove o arquivo temporário para não estourar o /tmp do Lambda
            if os.path.exists(filepath):
                os.remove(filepath)

    return {
        "statusCode": 200,
        "body": results
    }
