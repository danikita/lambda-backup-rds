# SQL Server (EC2) Backup to S3 via AWS Lambda + SSM

Versao do projeto adaptada para **SQL Server instalado em uma instancia EC2**
(em vez de RDS). Como a EC2 nao possui o procedimento `rds_backup_database`
(exclusivo do RDS), a arquitetura usa o **AWS Systems Manager (SSM) Run
Command** para executar um script PowerShell *dentro* da instancia, que faz
o `BACKUP DATABASE` nativo do SQL Server e envia o arquivo para o S3.

## Como funciona

1. **EventBridge** dispara a Lambda em um cron definido (igual ao cenario RDS).
2. A **Lambda** (`app.py`) chama `ssm:SendCommand` na instancia EC2, passando
   o tipo de backup (`FULL`/`DIFFERENTIAL`), bucket e prefixo do S3.
3. O **SSM Agent** (ja presente por padrao nas AMIs Windows da AWS) executa
   o script `backup_to_s3.ps1` na EC2.
4. O script:
   - Lista os bancos de usuario via `Invoke-Sqlcmd`.
   - Executa `BACKUP DATABASE ... TO DISK` (FULL ou `WITH DIFFERENTIAL`).
   - Faz `aws s3 cp` do arquivo `.bak` para o bucket S3.
   - Remove o arquivo local (opcional).
5. A Lambda faz polling do status do comando SSM (`GetCommandInvocation`) e
   retorna o resultado (sucesso/erro por banco).
6. **S3 Lifecycle** continua igual ao projeto RDS, com prefixos separados
   para `full/` e `differential/`.

```
EventBridge --> Lambda --> SSM SendCommand --> EC2 (SQL Server)
                                                  |-- BACKUP DATABASE (local disk)
                                                  '-- aws s3 cp --> S3
```

## Por que SSM em vez de conexao direta (pyodbc)?

- A EC2 ja tem o SQL Server local: nao ha necessidade de abrir porta 1433
  para a Lambda nem manter credenciais de conexao remota.
- O backup nativo `BACKUP DATABASE` precisa escrever em um caminho acessivel
  pelo proprio servico SQL Server (disco local), e o upload ao S3 e feito
  pela propria maquina — mais simples e seguro que a Lambda baixar/enviar.
- SSM Run Command e nativo da AWS, nao exige agente extra (SSM Agent ja vem
  nas AMIs Windows) e usa IAM, sem necessidade de chaves/segredos para a
  Lambda acessar a EC2.

---

## Pre-requisitos

### 1. IAM Role da instancia EC2

A instancia precisa de uma IAM Role com:

- `AmazonSSMManagedInstanceCore` — para o SSM Agent se comunicar.
- Permissao de escrita no bucket S3 de destino (`s3:PutObject`,
  `s3:GetBucketLocation`, etc).

### 2. AWS CLI na EC2

A maioria das AMIs Windows da AWS ja vem com o AWS CLI instalado. Caso nao
tenha, instale-o (o script usa `aws s3 cp`).

### 3. Modulo SqlServer / sqlcmd

O script usa `Invoke-Sqlcmd` (modulo `SqlServer` do PowerShell). Instale com:

```powershell
Install-Module -Name SqlServer -Scope AllUsers -Force
```

### 4. Copiar o script para a instancia

Copie `backup_to_s3.ps1` para um caminho fixo na EC2, por exemplo:

```
C:\Scripts\backup_to_s3.ps1
```

Isso pode ser feito via user-data no momento do provisionamento, AMI
customizada (golden image), ou um pipeline de configuracao (Ansible,
SSM State Manager, etc).

---

## Passo 1 — Preparar a EC2 (SQL Server)

```powershell
# Instalar modulo SqlServer
Install-Module -Name SqlServer -Scope AllUsers -Force

# Criar diretorio para o script e para backups temporarios
New-Item -ItemType Directory -Path C:\Scripts -Force
New-Item -ItemType Directory -Path C:\SQLBackups -Force

# Copiar backup_to_s3.ps1 para C:\Scripts\
```

Verifique se o SSM Agent esta rodando:

```powershell
Get-Service AmazonSSMAgent
```

---

## Passo 2 — Criar bucket S3 (se ainda nao existir)

```bash
aws s3 mb s3://meu-bucket-backups --region <region>
```

---

## Passo 3 — Criar a IAM Role da Lambda

```bash
aws iam create-role \
  --role-name lambda-role-ec2-backup \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Principal": { "Service": "lambda.amazonaws.com" },
        "Action": "sts:AssumeRole"
      }
    ]
  }'

aws iam attach-role-policy --role-name lambda-role-ec2-backup \
  --policy-arn arn:aws:iam::aws:policy/AWSLambdaBasicExecutionRole
```

Politica inline para permitir SSM:

```bash
aws iam put-role-policy \
  --role-name lambda-role-ec2-backup \
  --policy-name AllowSSMSendCommand \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": [
          "ssm:SendCommand",
          "ssm:GetCommandInvocation"
        ],
        "Resource": "*"
      }
    ]
  }'
```

> Para maior seguranca, restrinja `Resource` ao ARN da instancia EC2 e ao
> documento `AWS-RunPowerShellScript`.

---

## Passo 4 — Empacotar e publicar a Lambda

Esta Lambda **nao precisa de Docker** (sem dependencias externas alem do
`boto3`, que ja vem no runtime). Basta um zip simples:

```bash
zip lambda-ec2-backup.zip app.py

aws lambda create-function \
  --function-name lambda-sqlserver-ec2-backup \
  --runtime python3.12 \
  --handler app.lambda_handler \
  --zip-file fileb://lambda-ec2-backup.zip \
  --role arn:aws:iam::<account-id>:role/lambda-role-ec2-backup \
  --timeout 900 \
  --region <region>
```

> `--timeout 900` (15 min, maximo da Lambda) porque o backup pode demorar.
> Para backups muito grandes que excedam 15 minutos, use
> `"WAIT_FOR_COMPLETION": false` no payload e consulte o status do comando
> SSM separadamente (ou monitore via CloudWatch Logs do SSM).

---

## Passo 5 — Testar a invocacao

```bash
aws lambda invoke \
  --function-name lambda-sqlserver-ec2-backup \
  --cli-binary-format raw-in-base64-out \
  --payload '{
    "INSTANCE_ID": "i-0123456789abcdef0",
    "BACKUP_TYPE": "FULL",
    "S3_BUCKET": "meu-bucket-backups",
    "S3_PREFIX": "backups/full/",
    "SQL_INSTANCE": "localhost"
  }' \
  response.json

cat response.json
```

---

## Passo 6 — Agendar com EventBridge (igual ao projeto RDS)

### Full — semanal (domingo 1h UTC)

```bash
aws events put-rule \
  --name lambda-ec2-sqlserver-backup-full \
  --schedule-expression "cron(0 1 ? * SUN *)"

aws lambda add-permission \
  --function-name lambda-sqlserver-ec2-backup \
  --statement-id eventbridge-invoke-full \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:<region>:<account-id>:rule/lambda-ec2-sqlserver-backup-full

aws events put-targets \
  --rule lambda-ec2-sqlserver-backup-full \
  --targets '[{
    "Id": "full",
    "Arn": "arn:aws:lambda:<region>:<account-id>:function:lambda-sqlserver-ec2-backup",
    "Input": "{\"INSTANCE_ID\":\"i-0123456789abcdef0\",\"BACKUP_TYPE\":\"FULL\",\"S3_BUCKET\":\"meu-bucket-backups\",\"S3_PREFIX\":\"backups/full/\",\"SQL_INSTANCE\":\"localhost\"}"
  }]'
```

### Differential — diario (Seg-Sab 1h UTC)

```bash
aws events put-rule \
  --name lambda-ec2-sqlserver-backup-differential \
  --schedule-expression "cron(0 1 ? * MON-SAT *)"

aws lambda add-permission \
  --function-name lambda-sqlserver-ec2-backup \
  --statement-id eventbridge-invoke-differential \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:<region>:<account-id>:rule/lambda-ec2-sqlserver-backup-differential

aws events put-targets \
  --rule lambda-ec2-sqlserver-backup-differential \
  --targets '[{
    "Id": "differential",
    "Arn": "arn:aws:lambda:<region>:<account-id>:function:lambda-sqlserver-ec2-backup",
    "Input": "{\"INSTANCE_ID\":\"i-0123456789abcdef0\",\"BACKUP_TYPE\":\"DIFFERENTIAL\",\"S3_BUCKET\":\"meu-bucket-backups\",\"S3_PREFIX\":\"backups/differential/\",\"SQL_INSTANCE\":\"localhost\"}"
  }]'
```

---

## Passo 7 — Lifecycle Policy do S3 (igual ao projeto RDS)

```bash
aws s3api put-bucket-lifecycle-configuration \
  --bucket meu-bucket-backups \
  --lifecycle-configuration '{
    "Rules": [
      {
        "ID": "RetainFullBackups30Days",
        "Filter": { "Prefix": "backups/full/" },
        "Status": "Enabled",
        "Expiration": { "Days": 30 }
      },
      {
        "ID": "RetainDifferentialBackups7Days",
        "Filter": { "Prefix": "backups/differential/" },
        "Status": "Enabled",
        "Expiration": { "Days": 7 }
      }
    ]
  }'
```

---

## Diferencas principais vs. cenario RDS

| Aspecto                  | RDS (projeto original)                          | EC2 (este projeto)                                 |
|---------------------------|--------------------------------------------------|------------------------------------------------------|
| Quem faz o backup         | RDS, via `rds_backup_database` (assincrono)      | SQL Server local, via `BACKUP DATABASE` (sincrono)   |
| Quem envia ao S3          | RDS escreve direto no S3                          | Script PowerShell faz `aws s3 cp`                    |
| Conexao da Lambda          | pyodbc direto na porta 1433                       | Nenhuma — via SSM (sem porta aberta)                 |
| Imagem Docker / ECR        | Necessaria (driver ODBC)                          | Nao necessaria                                       |
| Onde roda o codigo de backup | Dentro da Lambda                               | Dentro da EC2 (via SSM Run Command)                  |
| Credenciais de banco       | Secrets Manager / DB_USER+PASSWORD                | `Invoke-Sqlcmd` usa autenticacao integrada (Windows) por padrao |
| IAM necessario p/ Lambda   | `secretsmanager`, `kms`, VPC, S3                  | Apenas `ssm:SendCommand` / `ssm:GetCommandInvocation` |
| IAM necessario p/ recurso  | Option Group + IAM Role no RDS                    | IAM Role na instancia EC2 (SSM + S3)                 |

---

## Notas e cuidados

- **Autenticacao SQL**: o script usa `Invoke-Sqlcmd -ServerInstance localhost`
  sem usuario/senha, ou seja, autenticacao Windows integrada (a conta em que
  o SSM executa o comando, normalmente `SYSTEM`, precisa ter permissao
  `sysadmin` ou ao menos `db_backupoperator` + `securityadmin` no SQL Server).
  Se preferir SQL Authentication, adicione `-Username`/`-Password` ao
  `Invoke-Sqlcmd` (idealmente lendo de Secrets Manager via AWS CLI/SDK
  dentro do proprio script PowerShell).
- **Espaco em disco**: o backup e gravado localmente antes do upload —
  garanta espaco suficiente em `LOCAL_DIR` (ou ajuste para um drive com
  mais espaco, ex: `D:\SQLBackups`).
- **LOG backups**: assim como no projeto RDS, backups de log de transacao
  exigem configuracao adicional (modelo de recuperacao FULL, agendamento
  proprio) e nao estao cobertos por este script.
- **Timeout da Lambda**: 15 minutos pode nao ser suficiente para bases
  muito grandes. Para esses casos, use `WAIT_FOR_COMPLETION=false` e
  monitore via CloudWatch Logs / SSM Run Command History.
