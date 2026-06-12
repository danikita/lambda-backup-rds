"""
Lambda: lambda-sqlserver-ec2-backup

Em vez de se conectar diretamente ao SQL Server (como no cenario RDS),
esta Lambda dispara um comando SSM (AWS-RunPowerShellScript) na instancia
EC2 que roda o SQL Server. O comando executa o script
'backup_to_s3.ps1', que faz o BACKUP DATABASE local e o upload para o S3.

Vantagens dessa abordagem:
  - Nao precisa de driver ODBC / pyodbc / Docker.
  - Nao precisa abrir a porta 1433 para a Lambda.
  - A Lambda so precisa de permissao ssm:SendCommand /
    ssm:GetCommandInvocation sobre a instancia.
  - O SQL Server faz o backup localmente e o AWS CLI (ou IAM Role da EC2)
    envia o arquivo para o S3 -- nada passa pela Lambda.

Payload de exemplo (evento):
{
  "INSTANCE_ID": "i-0123456789abcdef0",
  "BACKUP_TYPE": "FULL",
  "S3_BUCKET": "meu-bucket-backups",
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

# Script a ser executado via SSM. Deve corresponder ao backup_to_s3.ps1
# (mantido aqui para facilitar a chamada sem depender de arquivos externos
# no document do SSM. Alternativamente, use um documento SSM customizado
# que ja contenha este script e apenas referencie-o pelo nome).
DEFAULT_DOCUMENT_NAME = "AWS-RunPowerShellScript"

POLL_INTERVAL_SEC = 10
POLL_TIMEOUT_SEC = 1800  # 30 minutos (backups grandes podem demorar)


def build_command(event):
    """Monta o comando PowerShell que sera executado na EC2."""

    backup_type = event.get("BACKUP_TYPE", "FULL").upper()
    if backup_type not in VALID_BACKUP_TYPES:
        raise ValueError(
            f"BACKUP_TYPE invalido '{backup_type}'. Use FULL ou DIFFERENTIAL."
        )

    s3_bucket = event.get("S3_BUCKET")
    if not s3_bucket:
        raise ValueError("S3_BUCKET e obrigatorio no evento.")

    s3_prefix = event.get("S3_PREFIX", "")
    sql_instance = event.get("SQL_INSTANCE", "localhost")
    local_dir = event.get("LOCAL_DIR", r"C:\SQLBackups")
    retain_local = str(event.get("RETAIN_LOCAL", "false")).lower()

    # O script backup_to_s3.ps1 deve estar previamente copiado para a EC2
    # (ex: C:\Scripts\backup_to_s3.ps1) durante o provisionamento da maquina
    # (user-data, AMI customizada, ou pipeline de configuracao).
    script_path = event.get("SCRIPT_PATH", r"C:\Scripts\backup_to_s3.ps1")

    # Define as variaveis de ambiente do processo antes de chamar o script
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
            "body": ["Error: INSTANCE_ID e obrigatorio no evento."],
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
            Comment=f"SQL Server backup ({event.get('BACKUP_TYPE', 'FULL')}) para S3",
        )
    except Exception as e:
        return {"statusCode": 500, "body": [f"Error ao enviar comando SSM: {str(e)}"]}

    command_id = response["Command"]["CommandId"]

    if not wait:
        return {
            "statusCode": 200,
            "body": [
                f"[STARTED] command_id={command_id} instance={instance_id} "
                f"(execucao assincrona, nao aguardado)."
            ],
        }

    # Aguarda a conclusao do comando, fazendo polling no status
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
            # Ainda nao registrado, tenta novamente
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
        "body": [f"Timeout aguardando conclusao do command_id={command_id} (status={status})."],
    }
