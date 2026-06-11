import json
import time
import pyodbc
import boto3
import datetime
from zoneinfo import ZoneInfo


VALID_BACKUP_TYPES = {"FULL", "DIFFERENTIAL"}

# Intervalo e timeout para aguardar conclusão das tasks assíncronas do RDS
POLL_INTERVAL_SEC = 10
POLL_TIMEOUT_SEC  = 600   # 10 minutos


def get_db_credentials(event):
    secret_arn = event.get("SECRET_ARN", "").strip()

    if secret_arn.startswith("arn:"):
        secrets_client = boto3.client("secretsmanager", region_name="us-east-1")
        response = secrets_client.get_secret_value(SecretId=secret_arn)
        secret = json.loads(response["SecretString"])
        return secret["username"], secret["password"]

    db_user     = event.get("DB_USER")
    db_password = event.get("DB_PASSWORD")

    if not db_user or not db_password:
        raise ValueError("Provide either SECRET_ARN or DB_USER + DB_PASSWORD in the event.")

    return db_user, db_password


def wait_for_task(cursor, task_id, db_name, timeout=POLL_TIMEOUT_SEC):
    """
    Aguarda a conclusão de uma task assíncrona do RDS.
    Retorna (success: bool, message: str).
    """
    deadline = time.time() + timeout

    while time.time() < deadline:
        cursor.execute(
            "exec msdb.dbo.rds_task_status @task_id=?",
            task_id
        )
        row = cursor.fetchone()

        if row is None:
            return False, f"Task {task_id} não encontrada."

        # Colunas: task_id, task_type, lifecycle, created_at, last_updated, database_name,
        #          S3_object_arn, overwrite_s3_backup_file, KMS_master_key_arn,
        #          filepath, overwrite_s3_backup_file, error_message, ...
        # lifecycle fica na posição 2
        lifecycle    = (row[2] or "").strip().upper()
        error_msg    = row[10] if len(row) > 10 else None   # error_message

        if lifecycle == "SUCCESS":
            return True, f"Task {task_id} concluída com sucesso."

        if lifecycle in ("ERROR", "CANCELLED", "FAILED"):
            detail = error_msg or lifecycle
            return False, f"Task {task_id} falhou: {detail}"

        # Estados intermediários: CREATED, IN_PROGRESS, etc.
        time.sleep(POLL_INTERVAL_SEC)

    return False, f"Task {task_id} excedeu o timeout de {timeout}s (último lifecycle: {lifecycle})."


def lambda_handler(event, context):
    results = []

    try:
        DB_HOST     = event.get("DB_HOST")
        DB_PORT     = event.get("DB_PORT", "1433")
        S3_BUCKET   = event.get("S3_BUCKET")
        S3_PREFIX   = event.get("S3_PREFIX", "")
        BACKUP_TYPE = event.get("BACKUP_TYPE", "FULL").upper()
        WAIT        = event.get("WAIT_FOR_COMPLETION", True)  # aguarda por padrão

        if not DB_HOST or not S3_BUCKET:
            raise ValueError("DB_HOST and S3_BUCKET are required in the event.")

        if BACKUP_TYPE not in VALID_BACKUP_TYPES:
            raise ValueError(
                f"Invalid BACKUP_TYPE '{BACKUP_TYPE}'. Must be FULL or DIFFERENTIAL."
            )

        db_user, db_password = get_db_credentials(event)

        timestamp = datetime.datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%Y%m%d%H%M%S")

        ext_map  = {"FULL": "bak", "DIFFERENTIAL": "diff.bak"}
        file_ext = ext_map[BACKUP_TYPE]

        conn_str = (
            f"DRIVER={{ODBC Driver 17 for SQL Server}};"
            f"SERVER={DB_HOST},{DB_PORT};"
            f"UID={db_user};"
            f"PWD={db_password};"
            f"Encrypt=no;"
        )

        conn   = pyodbc.connect(conn_str)
        cursor = conn.cursor()

        # Para DIFFERENTIAL: verifica se existe full backup anterior.
        # Um snapshot automático/manual entre o último full e agora
        # também bloqueia o differential — nesse caso é necessário refazer o FULL.
        if BACKUP_TYPE == "DIFFERENTIAL":
            cursor.execute("""
                SELECT TOP 1 database_name
                FROM msdb.dbo.backupset
                WHERE type = 'D'
                  AND database_name NOT IN ('master','tempdb','model','msdb','rdsadmin')
                ORDER BY backup_finish_date DESC
            """)
            if cursor.fetchone() is None:
                raise ValueError(
                    "Nenhum FULL backup encontrado. "
                    "Execute um FULL backup antes do DIFFERENTIAL."
                )

        cursor.execute("""
            SELECT name
            FROM sys.databases
            WHERE name NOT IN ('master', 'tempdb', 'model', 'msdb', 'rdsadmin')
        """)
        databases = [row[0] for row in cursor.fetchall()]

        for db in databases:
            s3_arn = (
                f"arn:aws:s3:::{S3_BUCKET}/{S3_PREFIX}"
                f"{db}_{BACKUP_TYPE}_{timestamp}.{file_ext}"
            )

            sql = (
                "exec msdb.dbo.rds_backup_database "
                f"  @source_db_name='{db}', "
                f"  @s3_arn_to_backup_to='{s3_arn}', "
                f"  @type='{BACKUP_TYPE}', "
                "   @overwrite_S3_backup_file=1;"
            )

            try:
                cursor.execute(sql)

                # A proc devolve um result-set com task_id na primeira coluna
                row     = cursor.fetchone()
                task_id = row[0] if row else None

                conn.commit()

                if task_id and WAIT:
                    success, msg = wait_for_task(cursor, task_id, db)
                    status = "OK" if success else "FAILED"
                    results.append(
                        f"[{BACKUP_TYPE}][{status}] {db} → {s3_arn} | task_id={task_id} | {msg}"
                    )
                else:
                    results.append(
                        f"[{BACKUP_TYPE}][STARTED] {db} → {s3_arn} | task_id={task_id}"
                    )

            except Exception as e:
                results.append(f"[{BACKUP_TYPE}][ERROR] {db}: {str(e)}")

    except Exception as e:
        results.append(f"Error: {str(e)}")

    all_ok = results and all(
        "[ERROR]" not in r and "[FAILED]" not in r and "Error:" not in r
        for r in results
    )

    return {
        "statusCode": 200 if all_ok else 500,
        "body": results,
    }
