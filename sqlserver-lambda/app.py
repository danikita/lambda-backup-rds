import os
import json
import pyodbc
import boto3
import datetime
from zoneinfo import ZoneInfo

def load_config():
    base_path = os.getcwd()
    file_path = os.path.join(base_path, "config.txt")

    config = {}
    with open(file_path, "r") as file:
        for line in file:
            if "=" in line:
                key, value = line.strip().split("=", 1)
                config[key] = value
    return config


config = load_config()

DB_HOST = config.get("DB_HOST")
DB_PORT = config.get("DB_PORT")
S3_BUCKET = config.get("S3_BUCKET")
S3_PREFIX = config.get("S3_PREFIX")


def get_db_credentials():

    # Se SECRET_ARN estiver preenchido, usa Secrets Manager
    if config.get("SECRET_ARN"):
        secrets_client = boto3.client("secretsmanager", region_name="us-east-1")
        response = secrets_client.get_secret_value(SecretId=config.get("SECRET_ARN"))
        secret = json.loads(response["SecretString"])
        return secret["username"], secret["password"]

    # Caso contrário, usa usuário/senha manual
    return (
        config.get("DB_USER"),
        config.get("DB_PASSWORD")
    )


# Timestamp para nome do arquivo
timestamp = datetime.datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%Y%m%d%H%M%S")


# Main Lambda function
def lambda_handler(event, context):
    results = []

    try:
        # ✅ CORRIGIDO: sem passar parâmetro
        db_user, db_password = get_db_credentials()

        conn_str = (
            f"DRIVER={{ODBC Driver 18 for SQL Server}};"
            f"SERVER={DB_HOST},{DB_PORT};"
            f"UID={db_user};"
            f"PWD={db_password};"
            f"Encrypt=no;"
        )

        conn = pyodbc.connect(conn_str)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT name 
            FROM sys.databases 
            WHERE name NOT IN ('master', 'tempdb', 'model', 'msdb', 'rdsadmin')
        """)
        databases = [row[0] for row in cursor.fetchall()]

        for db in databases:
            s3_arn = f"arn:aws:s3:::{S3_BUCKET}/{S3_PREFIX}{db}_{timestamp}.bak"

            sql = f"""
            exec msdb.dbo.rds_backup_database 
                @source_db_name='{db}', 
                @s3_arn_to_backup_to='{s3_arn}', 
                @overwrite_S3_backup_file=1;
            """

            try:
                cursor.execute(sql)
                conn.commit()
                results.append(f"Backup iniciado para {db} → {s3_arn}")
            except Exception as e:
                results.append(f"Erro ao iniciar backup de {db}: {str(e)}")

    except Exception as e:
        results.append(f"Erro de conexão ou credenciais: {str(e)}")

    return {
        "statusCode": 200 if all("OK" in r for r in results) else 500,
        "body": results
    }
