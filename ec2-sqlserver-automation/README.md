# SQL Server (EC2) Backup to S3 via AWS Lambda + SSM

Version of the project adapted for **SQL Server installed on an EC2
instance** (instead of RDS). Since EC2 does not have the
`rds_backup_database` procedure (exclusive to RDS), the architecture uses
**AWS Systems Manager (SSM) Run Command** to execute a PowerShell script
*inside* the instance, which performs the native SQL Server `BACKUP
DATABASE` and uploads the file to S3.

## How it works

1. **EventBridge** triggers the Lambda on a defined cron (same as the RDS scenario).
2. The **Lambda** (`app.py`) calls `ssm:SendCommand` on the EC2 instance,
   passing the backup type (`FULL`/`DIFFERENTIAL`/`LOG`), S3 bucket, and prefix.
3. The **SSM Agent** (present by default on AWS Windows AMIs) executes the
   `backup_to_s3.ps1` script on the EC2 instance.
4. The script:
   - Lists user databases via `Invoke-Sqlcmd`.
   - Runs `BACKUP DATABASE ... TO DISK` (FULL or `WITH DIFFERENTIAL`), or
     `BACKUP LOG ... TO DISK` for transaction log backups.
   - Runs `aws s3 cp` to upload the `.bak`/`.trn` file to the S3 bucket.
   - Removes the local file (optional).
5. The Lambda polls the SSM command status (`GetCommandInvocation`) and
   returns the result (success/error per database).
6. **S3 Lifecycle** remains the same as the RDS project, with separate
   prefixes for `full/`, `differential/`, and `log/`.

```
EventBridge --> Lambda --> SSM SendCommand --> EC2 (SQL Server)
                                                  |-- BACKUP DATABASE (local disk)
                                                  '-- aws s3 cp --> S3
```

## Why SSM instead of a direct connection (pyodbc)?

- The EC2 instance already has SQL Server installed locally: there's no need
  to open port 1433 to the Lambda or keep remote connection credentials.
- A native `BACKUP DATABASE` needs to write to a path accessible by the SQL
  Server service itself (local disk), and the upload to S3 is done by the
  machine itself — simpler and safer than having the Lambda download/upload
  the file.
- SSM Run Command is native to AWS, requires no extra agent (the SSM Agent
  already ships with Windows AMIs), and uses IAM, without needing
  keys/secrets for the Lambda to access the EC2 instance.

---

## Prerequisites

### 1. EC2 instance IAM Role

The instance needs an IAM Role with:

- `AmazonSSMManagedInstanceCore` — so the SSM Agent can communicate.
- Write permission to the destination S3 bucket (`s3:PutObject`,
  `s3:GetBucketLocation`, etc).

### 2. AWS CLI on the EC2 instance

Most AWS Windows AMIs already include the AWS CLI. If not, install it (the
script uses `aws s3 cp`).

### 3. SqlServer module / sqlcmd

The script uses `Invoke-Sqlcmd` (the PowerShell `SqlServer` module). Install
it with:

```powershell
Install-Module -Name SqlServer -Scope AllUsers -Force
```

### 4. Copy the script to the instance

Copy `backup_to_s3.ps1` to a fixed path on the EC2 instance, for example:

```
C:\Scripts\backup_to_s3.ps1
```

This can be done via user-data at provisioning time, a custom AMI (golden
image), or a configuration pipeline (Ansible, SSM State Manager, etc).

---

## Step 1 — Prepare the EC2 instance (SQL Server)

```powershell
# Install the SqlServer module
Install-Module -Name SqlServer -Scope AllUsers -Force

# Create directories for the script and temporary backups
New-Item -ItemType Directory -Path C:\Scripts -Force
New-Item -ItemType Directory -Path C:\SQLBackups -Force

# Copy backup_to_s3.ps1 to C:\Scripts\
```

Check that the SSM Agent is running:

```powershell
Get-Service AmazonSSMAgent
```

---

## Step 2 — Create the S3 bucket (if it doesn't exist yet)

```bash
aws s3 mb s3://my-backups-bucket --region <region>
```

---

## Step 3 — Create the Lambda IAM Role

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

Inline policy to allow SSM:

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

> For better security, restrict `Resource` to the EC2 instance ARN and to
> the `AWS-RunPowerShellScript` document. See `iam_policies.md` for the
> hardened version.

---

## Step 4 — Package and publish the Lambda

This Lambda **does not need Docker** (no dependencies beyond `boto3`, which
is already included in the runtime). Just a simple zip:

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

> `--timeout 900` (15 minutes, the Lambda maximum) because the backup may
> take a while. For very large backups that exceed 15 minutes, use
> `"WAIT_FOR_COMPLETION": false` in the payload and check the SSM command
> status separately (or monitor via the SSM Run Command's CloudWatch Logs).

---

## Step 5 — Test the invocation

```bash
aws lambda invoke \
  --function-name lambda-sqlserver-ec2-backup \
  --cli-binary-format raw-in-base64-out \
  --payload '{
    "INSTANCE_ID": "i-0123456789abcdef0",
    "BACKUP_TYPE": "FULL",
    "S3_BUCKET": "my-backups-bucket",
    "S3_PREFIX": "backups/full/",
    "SQL_INSTANCE": "localhost"
  }' \
  response.json

cat response.json
```

---

## Step 6 — Schedule with EventBridge (same as the RDS project)

### Full — weekly (Sunday at 1 AM UTC)

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
    "Input": "{\"INSTANCE_ID\":\"i-0123456789abcdef0\",\"BACKUP_TYPE\":\"FULL\",\"S3_BUCKET\":\"my-backups-bucket\",\"S3_PREFIX\":\"backups/full/\",\"SQL_INSTANCE\":\"localhost\"}"
  }]'
```

### Differential — daily (Mon-Sat at 1 AM UTC)

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
    "Input": "{\"INSTANCE_ID\":\"i-0123456789abcdef0\",\"BACKUP_TYPE\":\"DIFFERENTIAL\",\"S3_BUCKET\":\"my-backups-bucket\",\"S3_PREFIX\":\"backups/differential/\",\"SQL_INSTANCE\":\"localhost\"}"
  }]'
```

### Log — hourly (every hour, all days)

```bash
aws events put-rule \
  --name lambda-ec2-sqlserver-backup-log \
  --schedule-expression "cron(0 * ? * * *)"

aws lambda add-permission \
  --function-name lambda-sqlserver-ec2-backup \
  --statement-id eventbridge-invoke-log \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:<region>:<account-id>:rule/lambda-ec2-sqlserver-backup-log

aws events put-targets \
  --rule lambda-ec2-sqlserver-backup-log \
  --targets '[{
    "Id": "log",
    "Arn": "arn:aws:lambda:<region>:<account-id>:function:lambda-sqlserver-ec2-backup",
    "Input": "{\"INSTANCE_ID\":\"i-0123456789abcdef0\",\"BACKUP_TYPE\":\"LOG\",\"S3_BUCKET\":\"my-backups-bucket\",\"S3_PREFIX\":\"backups/log/\",\"SQL_INSTANCE\":\"localhost\"}"
  }]'
```

> Log backups only work for databases in the `FULL` or `BULK_LOGGED`
> recovery model and require a prior `FULL` backup. Databases in `SIMPLE`
> recovery model are automatically skipped by the script (see "Notes and
> caveats" below).

---

## Step 7 — S3 Lifecycle Policy (same as the RDS project)

```bash
aws s3api put-bucket-lifecycle-configuration \
  --bucket my-backups-bucket \
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
      },
      {
        "ID": "RetainLogBackups3Days",
        "Filter": { "Prefix": "backups/log/" },
        "Status": "Enabled",
        "Expiration": { "Days": 3 }
      }
    ]
  }'
```

---

## Key differences vs. the RDS scenario

| Aspect                      | RDS (original project)                          | EC2 (this project)                                     |
|-------------------------------|----------------------------------------------------|-------------------------------------------------------------|
| Who performs the backup       | RDS, via `rds_backup_database` (asynchronous)        | Local SQL Server, via `BACKUP DATABASE` (synchronous)         |
| Who uploads to S3              | RDS writes directly to S3                            | PowerShell script runs `aws s3 cp`                            |
| Lambda connection              | pyodbc directly on port 1433                          | None — via SSM (no open port)                                 |
| Docker image / ECR             | Required (ODBC driver)                               | Not required                                                  |
| Where the backup code runs     | Inside the Lambda                                    | Inside the EC2 instance (via SSM Run Command)                 |
| Database credentials           | Secrets Manager / DB_USER+PASSWORD                    | `Invoke-Sqlcmd` uses integrated (Windows) auth by default      |
| IAM required for the Lambda    | `secretsmanager`, `kms`, VPC, S3                      | Only `ssm:SendCommand` / `ssm:GetCommandInvocation`            |
| IAM required for the resource  | Option Group + IAM Role on RDS                        | IAM Role on the EC2 instance (SSM + S3)                        |

---

## Notes and caveats

- **SQL authentication**: the script uses `Invoke-Sqlcmd -ServerInstance
  localhost` without a username/password, i.e. integrated Windows
  authentication (the account the SSM command runs as, typically `SYSTEM`,
  needs `sysadmin`, or at least `db_backupoperator` + `securityadmin`,
  privileges on SQL Server). If you prefer SQL Authentication, add
  `-Username`/`-Password` to `Invoke-Sqlcmd` (ideally reading from Secrets
  Manager via the AWS CLI/SDK inside the PowerShell script itself).
- **Disk space**: the backup is written locally before the upload — make
  sure there's enough space in `LOCAL_DIR` (or point it to a drive with more
  space, e.g. `D:\SQLBackups`).
- **Transaction log backups**: supported via `BACKUP_TYPE=LOG`. The script
  automatically skips databases in `SIMPLE` recovery model (log backups
  aren't applicable) and databases without a prior `FULL` backup. Make sure
  affected databases use the `FULL` or `BULK_LOGGED` recovery model, and
  schedule the `LOG` rule frequently enough (e.g. every 15-60 minutes) to
  meet your RPO and keep the transaction log from growing too large.
- **Lambda timeout**: 15 minutes may not be enough for very large databases.
  In those cases, use `WAIT_FOR_COMPLETION=false` and monitor via CloudWatch
  Logs / SSM Run Command History.
