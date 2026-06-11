# SQL Server RDS Backup to S3 via AWS Lambda

This project automates native backups of SQL Server RDS instances to Amazon S3 using a serverless, container-based architecture.

The diagram below illustrates how the components interact:

<img width="932" height="551" alt="image" src="https://github.com/user-attachments/assets/4f740d75-bc45-416f-aa91-03d51b84396e" />

### How it works

The solution is built around an AWS Lambda function packaged as a Docker image and deployed via Amazon ECR. The function connects directly to the RDS SQL Server instance, triggers a native backup using the `rds_backup_database` stored procedure, and writes the backup file straight to an S3 bucket — no intermediate storage required.

An **EC2 Bastion** host is used during the initial setup phase to build the Docker image and push it to ECR. Once the Lambda function is deployed, the bastion is no longer needed for day-to-day operation.

**EventBridge** scheduler rules are used to trigger the Lambda automatically on a defined schedule, supporting three independent backup tiers: full, differential, and transaction log — each with its own frequency and S3 prefix.

The backup file lands directly in **S3**, where lifecycle policies can be applied per backup type to automatically expire old files and control storage costs.

### Components

- **EC2 Bastion** — used once at setup to build and push the container image to ECR
- **ECR** — stores the Docker image that packages the Lambda runtime and the ODBC driver for SQL Server
- **Lambda** — executes the backup logic: connects to RDS, triggers the native backup, and writes the `.bak`, `.diff.bak`, or `.trn` file to S3
- **EventBridge** — schedules the Lambda invocations with different backup types and frequencies
- **RDS SQL Server** — the source database; must have the `SQLSERVER_BACKUP_RESTORE` option group enabled
- **S3** — the backup destination; organized by prefix per backup type, with lifecycle rules for automated retention

---

## Prerequisites

### RDS Option Group

Your RDS instance must have a **custom Option Group** with the `SQLSERVER_BACKUP_RESTORE` option enabled, and an IAM role with S3 write permissions must be attached to it.

> See the [AWS documentation](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/SQLServer.Procedural.Importing.Native.Enabling.html) for setup instructions.

---

## Step 1 — Set Up a Bastion EC2 (Amazon Linux)

Launch an Amazon Linux EC2 instance to use as a bastion host, then install the required dependencies:

```bash
sudo yum update -y
sudo yum install docker -y
sudo service docker start
sudo usermod -a -G docker ec2-user
sudo yum install git -y
```

---

## Step 2 — Create an IAM User

Create a dedicated IAM user and attach the required policies:

```bash
aws iam create-user --user-name backup-user

aws iam attach-user-policy --user-name backup-user --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess
aws iam attach-user-policy --user-name backup-user --policy-arn arn:aws:iam::aws:policy/AmazonRDSFullAccess
aws iam attach-user-policy --user-name backup-user --policy-arn arn:aws:iam::aws:policy/AmazonEC2FullAccess
aws iam attach-user-policy --user-name backup-user --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryFullAccess
aws iam attach-user-policy --user-name backup-user --policy-arn arn:aws:iam::aws:policy/AmazonEventBridgeFullAccess
```

Then create and save the access key credentials:

```bash
aws iam create-access-key --user-name backup-user
```

---

## Step 3 — Clone the Repository and Configure AWS CLI

SSH into the bastion host, clone this repository, and configure the AWS CLI with the credentials from Step 2:

```bash
git clone https://github.com/danikita/lambda-backup-rds
aws configure

cd lambda-backup-rds/sqlserver-lambda
```

---

## Step 4 — Build and Push the Docker Image to ECR

**Create the ECR repository:**

```bash
aws ecr create-repository \
  --repository-name lambda-sqlserver-backup \
  --region <region>
```

**Authenticate Docker with ECR:**

```bash
aws ecr get-login-password --region <region> \
  | docker login --username AWS --password-stdin <account-id>.dkr.ecr.<region>.amazonaws.com
```

**Build, tag, and push the image:**

```bash
docker build -t lambda-sqlserver-backup .

docker tag lambda-sqlserver-backup:latest \
  <account-id>.dkr.ecr.<region>.amazonaws.com/lambda-sqlserver-backup:latest

docker push \
  <account-id>.dkr.ecr.<region>.amazonaws.com/lambda-sqlserver-backup:latest
```

---

## Step 5 — Create the Lambda IAM Role

```bash
aws iam create-role \
  --role-name lambda-role-backup \
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
```

**Attach managed policies:**

```bash
aws iam attach-role-policy --role-name lambda-role-backup --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess
aws iam attach-role-policy --role-name lambda-role-backup --policy-arn arn:aws:iam::aws:policy/AWSLambdaBasicExecutionRole
aws iam attach-role-policy --role-name lambda-role-backup --policy-arn arn:aws:iam::aws:policy/SecretsManagerReadWrite
aws iam attach-role-policy --role-name lambda-role-backup --policy-arn arn:aws:iam::aws:policy/AWSLambdaVPCAccessExecutionRole
```

**Attach inline policy for Secrets Manager access:**

```bash
aws iam put-role-policy \
  --role-name lambda-role-backup \
  --policy-name AllowSecretsManagerAccess \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": "secretsmanager:GetSecretValue",
        "Resource": "arn:aws:secretsmanager:<region>:<account-id>:secret:secret-rds-sqlserver-*"
      }
    ]
  }'
```

**Attach inline policy for KMS decryption:**

```bash
aws iam put-role-policy \
  --role-name lambda-role-backup \
  --policy-name AllowKMS \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": ["kms:Decrypt"],
        "Resource": "*"
      }
    ]
  }'
```

---

## Step 6 — Deploy the Lambda Function

```bash
aws lambda create-function \
  --function-name lambda-sqlserver-backup \
  --package-type Image \
  --code ImageUri=<account-id>.dkr.ecr.<region>.amazonaws.com/lambda-sqlserver-backup:latest \
  --role arn:aws:iam::<account-id>:role/lambda-role-backup \
  --timeout 30 \
  --vpc-config SubnetIds=<subnet-xx>,<subnet-yy>,SecurityGroupIds=<sg-zz> \
  --region <region>
```

---

## Step 7 — Invoke the Lambda Function

### Option A — Using AWS Secrets Manager (recommended)

```bash
aws lambda invoke \
  --function-name lambda-sqlserver-backup \
  --cli-binary-format raw-in-base64-out \
  --payload '{
    "DB_HOST": "<rds-endpoint>",
    "DB_PORT": "<port>",
    "SECRET_ARN": "<secrets-manager-arn>",
    "S3_BUCKET": "<bucket-name>",
    "S3_PREFIX": "<prefix>/",
    "BACKUP_TYPE": "<FULL/DIFFERENTIAL/LOG>"
  }' \
  response.json \
&& aws logs tail /aws/lambda/lambda-sqlserver-backup --follow
```

### Option B — Using plain credentials

Use this option if your RDS instance is not integrated with Secrets Manager:

```bash
aws lambda invoke \
  --function-name lambda-sqlserver-backup \
  --cli-binary-format raw-in-base64-out \
  --payload '{
    "DB_HOST": "<rds-endpoint>",
    "DB_PORT": "<port>",
    "DB_USER": "<db-user>",
    "DB_PASSWORD": "<db-password>",
    "S3_BUCKET": "<bucket-name>",
    "S3_PREFIX": "<prefix>/",
    "BACKUP_TYPE": "<FULL/DIFFERENTIAL/LOG>"
  }' \
  response.json \
&& aws logs tail /aws/lambda/lambda-sqlserver-backup --follow
```

The `--follow` flag streams CloudWatch logs in real time so you can verify whether the backup completed successfully.

## Backup Types

The Lambda supports three backup types via the `BACKUP_TYPE` field in the event payload:

| Value          | Description                                                                 | File extension |
|----------------|-----------------------------------------------------------------------------|----------------|
| `FULL`         | Complete database backup. Default if `BACKUP_TYPE` is omitted.              | `.bak`         |
| `DIFFERENTIAL` | Only changes since the last FULL backup. Requires a prior FULL backup.      | `.diff.bak`    |
| `LOG`          | Transaction log backup. Requires Full Recovery Model and a prior FULL backup.| `.trn`         |

> **Important:** `DIFFERENTIAL` and `LOG` backups depend on a `FULL` backup existing first.  
> `LOG` backups additionally require the database to be in **Full Recovery Model**.

---

## ⚠️ Networking Considerations

Before invoking the function, make sure the Lambda and the RDS instance are in the **same VPC** (or connected VPCs), and that the **Lambda's security group** is allowed in the RDS inbound rules on the SQL Server port (default: `1433`).

Without proper network connectivity between Lambda and RDS, the backup will fail with a connection timeout.

---

## Extra Step — Automate with EventBridge (Scheduled Backups)

You can schedule the Lambda to run automatically using an EventBridge rule with a cron expression.

### 1. Create the rule

The example below schedules the backup every **Monday at 10:00 PM UTC**:

```bash
aws events put-rule \
  --name lambda-sqlserver-backup-weekly \
  --schedule-expression "cron(0 22 ? * MON *)"
```

Adjust the cron expression to match your desired schedule. See the [AWS cron expression reference](https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html#cron-based) for syntax details.

### 2. Grant EventBridge permission to invoke the Lambda

```bash
aws lambda add-permission \
  --function-name lambda-sqlserver-backup \
  --statement-id eventbridge-invoke \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:<region>:<account-id>:rule/lambda-sqlserver-backup-weekly
```

### 3. Set the Lambda as the rule target

```bash
aws events put-targets \
  --rule lambda-sqlserver-backup-weekly \
  --targets "Id"="1","Arn"="arn:aws:lambda:<region>:<account-id>:function:lambda-sqlserver-backup"
```

Once configured, EventBridge will automatically trigger the backup function on the defined schedule without any manual invocation.

## Suggested EventBridge Schedule (Multi-Tier Strategy)

A common backup strategy uses **three separate EventBridge rules pointing to the same Lambda**, each passing a different `BACKUP_TYPE` in the input:

| Rule                   | Type         | Suggested frequency       |
|------------------------|--------------|---------------------------|
| Full backup            | `FULL`       | Weekly (e.g. Sunday 1 AM) |
| Differential backup    | `DIFFERENTIAL` | Daily (e.g. every day 1 AM, except Sunday) |
| Log backup             | `LOG`        | Every few hours (e.g. every 4 hours) |

### 1. Full backup — weekly (Sunday at 1:00 AM UTC)

```bash
aws events put-rule \
  --name lambda-sqlserver-backup-full \
  --schedule-expression "cron(0 1 ? * SUN *)"

aws lambda add-permission \
  --function-name lambda-sqlserver-backup \
  --statement-id eventbridge-invoke-full \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:<region>:<account-id>:rule/lambda-sqlserver-backup-full

aws events put-targets \
  --rule lambda-sqlserver-backup-full \
  --targets '[{
    "Id": "full",
    "Arn": "arn:aws:lambda:<region>:<account-id>:function:lambda-sqlserver-backup",
    "Input": "{\"DB_HOST\":\"<rds-endpoint>\",\"DB_PORT\":\"1433\",\"SECRET_ARN\":\"<secrets-manager-arn>\",\"S3_BUCKET\":\"<bucket-name>\",\"S3_PREFIX\":\"backups/\",\"BACKUP_TYPE\":\"FULL\"}"
  }]'
```

### 2. Differential backup — daily (Mon–Sat at 1:00 AM UTC)

```bash
aws events put-rule \
  --name lambda-sqlserver-backup-differential \
  --schedule-expression "cron(0 1 ? * MON-SAT *)"

aws lambda add-permission \
  --function-name lambda-sqlserver-backup \
  --statement-id eventbridge-invoke-differential \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:<region>:<account-id>:rule/lambda-sqlserver-backup-differential

aws events put-targets \
  --rule lambda-sqlserver-backup-differential \
  --targets '[{
    "Id": "differential",
    "Arn": "arn:aws:lambda:<region>:<account-id>:function:lambda-sqlserver-backup",
    "Input": "{\"DB_HOST\":\"<rds-endpoint>\",\"DB_PORT\":\"1433\",\"SECRET_ARN\":\"<secrets-manager-arn>\",\"S3_BUCKET\":\"<bucket-name>\",\"S3_PREFIX\":\"backups/\",\"BACKUP_TYPE\":\"DIFFERENTIAL\"}"
  }]'
```

### 3. Log backup — every 4 hours

```bash
aws events put-rule \
  --name lambda-sqlserver-backup-log \
  --schedule-expression "rate(4 hours)"

aws lambda add-permission \
  --function-name lambda-sqlserver-backup \
  --statement-id eventbridge-invoke-log \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:<region>:<account-id>:rule/lambda-sqlserver-backup-log

aws events put-targets \
  --rule lambda-sqlserver-backup-log \
  --targets '[{
    "Id": "log",
    "Arn": "arn:aws:lambda:<region>:<account-id>:function:lambda-sqlserver-backup",
    "Input": "{\"DB_HOST\":\"<rds-endpoint>\",\"DB_PORT\":\"1433\",\"SECRET_ARN\":\"<secrets-manager-arn>\",\"S3_BUCKET\":\"<bucket-name>\",\"S3_PREFIX\":\"backups/\",\"BACKUP_TYPE\":\"LOG\"}"
  }]'
```

> Adjust schedules and the `Input` payload to match your environment. The `Input` field is how EventBridge passes the payload to the Lambda when triggered automatically — it replaces the `--payload` used in manual invocations.

---

---

## Extra Step — S3 Lifecycle Policy (Automatic Backup Retention)

Since full, differential, and log backups have different sizes and recovery purposes, it makes sense to apply **different retention periods per backup type**. The file naming convention used by this Lambda (`FULL`, `DIFFERENTIAL`, `LOG` in the filename) makes it straightforward to target each type with a separate lifecycle rule using S3 prefixes.

### Suggested retention policy

| Backup type    | Suggested retention | Rationale                                                      |
|----------------|---------------------|----------------------------------------------------------------|
| `FULL`         | 30 days             | Kept longer as the base for any restore chain                  |
| `DIFFERENTIAL` | 7 days              | Only useful between two FULL backups                           |
| `LOG`          | 2 days              | High frequency; only needed for point-in-time recovery windows |

### AWS CLI command

The rule below applies all three retention policies at once, assuming your `S3_PREFIX` is `backups/`:

```bash
aws s3api put-bucket-lifecycle-configuration \
  --bucket <bucket-name> \
  --lifecycle-configuration '{
    "Rules": [
      {
        "ID": "RetainFullBackups30Days",
        "Filter": { "Prefix": "backups/" },
        "Status": "Enabled",
        "Expiration": { "Days": 30 }
      },
      {
        "ID": "RetainDifferentialBackups7Days",
        "Filter": { "Prefix": "backups/" },
        "Status": "Enabled",
        "Expiration": { "Days": 7 }
      },
      {
        "ID": "RetainLogBackups2Days",
        "Filter": { "Prefix": "backups/" },
        "Status": "Enabled",
        "Expiration": { "Days": 2 }
      }
    ]
  }'
```

> **Note:** For the prefix-based rules above to correctly target each backup type independently, the cleanest approach is to use **separate S3 prefixes per type** — e.g. `backups/full/`, `backups/differential/`, `backups/log/`. To do this, set `S3_PREFIX` accordingly in each EventBridge rule's `Input` payload. This way each lifecycle rule targets only its own prefix with no overlap.

### Example with separate prefixes

```bash
aws s3api put-bucket-lifecycle-configuration \
  --bucket <bucket-name> \
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
        "ID": "RetainLogBackups2Days",
        "Filter": { "Prefix": "backups/log/" },
        "Status": "Enabled",
        "Expiration": { "Days": 2 }
      }
    ]
  }'
```

And the corresponding EventBridge `Input` payloads would use:

- `"S3_PREFIX": "backups/full/"` for the FULL rule
- `"S3_PREFIX": "backups/differential/"` for the DIFFERENTIAL rule
- `"S3_PREFIX": "backups/log/"` for the LOG rule

Adjust the retention days to match your organization's recovery point objectives (RPO).
