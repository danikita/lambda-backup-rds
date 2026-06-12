# Required IAM Policies

This project involves two distinct IAM Roles: the **EC2 instance Role**
(where SQL Server runs) and the **Lambda Role** (which triggers the SSM Run
Command).

---

## 1. EC2 instance IAM Role

The instance needs an **Instance Profile** with the following role attached.

### 1.1. Trust policy (assume role)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "ec2.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

```bash
aws iam create-role \
  --role-name ec2-role-sqlserver-backup \
  --assume-role-policy-document file://ec2-trust-policy.json
```

### 1.2. Managed policy — SSM

Required so the SSM Agent can communicate with Systems Manager and receive
the Run Command:

```bash
aws iam attach-role-policy \
  --role-name ec2-role-sqlserver-backup \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
```

### 1.3. Inline policy — S3 access (uploading backups)

```bash
aws iam put-role-policy \
  --role-name ec2-role-sqlserver-backup \
  --policy-name AllowS3BackupUpload \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": [
          "s3:PutObject",
          "s3:PutObjectAcl"
        ],
        "Resource": "arn:aws:s3:::<bucket-name>/*"
      },
      {
        "Effect": "Allow",
        "Action": [
          "s3:GetBucketLocation",
          "s3:ListBucket"
        ],
        "Resource": "arn:aws:s3:::<bucket-name>"
      }
    ]
  }'
```

### 1.4. Create and attach the Instance Profile

```bash
aws iam create-instance-profile \
  --instance-profile-name ec2-instance-profile-sqlserver-backup

aws iam add-role-to-instance-profile \
  --instance-profile-name ec2-instance-profile-sqlserver-backup \
  --role-name ec2-role-sqlserver-backup

aws ec2 associate-iam-instance-profile \
  --instance-id <instance-id> \
  --iam-instance-profile Name=ec2-instance-profile-sqlserver-backup
```

> If the instance already exists without an instance profile, use
> `associate-iam-instance-profile` as shown above. If it already has a
> profile, use `replace-iam-instance-profile-association`.

---

## 2. Lambda IAM Role

### 2.1. Trust policy (assume role)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "lambda.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

```bash
aws iam create-role \
  --role-name lambda-role-ec2-backup \
  --assume-role-policy-document file://lambda-trust-policy.json
```

### 2.2. Managed policy — basic execution (CloudWatch Logs)

```bash
aws iam attach-role-policy \
  --role-name lambda-role-ec2-backup \
  --policy-arn arn:aws:iam::aws:policy/AWSLambdaBasicExecutionRole
```

### 2.3. Inline policy — SSM Run Command

The Lambda needs to send the command and check the invocation result.
Below is the **restricted** version (recommended), limiting the action to
the specific instance and to the `AWS-RunPowerShellScript` document:

```bash
aws iam put-role-policy \
  --role-name lambda-role-ec2-backup \
  --policy-name AllowSSMSendCommand \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": "ssm:SendCommand",
        "Resource": [
          "arn:aws:ec2:<region>:<account-id>:instance/<instance-id>",
          "arn:aws:ssm:<region>::document/AWS-RunPowerShellScript"
        ]
      },
      {
        "Effect": "Allow",
        "Action": "ssm:GetCommandInvocation",
        "Resource": "*"
      }
    ]
  }'
```

> `ssm:GetCommandInvocation` does not support restriction by a specific
> resource ARN, hence `Resource: "*"` for that action. To narrow the scope
> further, restrict `ssm:SendCommand` using tags/conditions.

#### Simplified version (less restrictive)

If you prefer to allow any instance/document (useful for test environments,
but avoid in production):

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

---

## 3. Permissions summary

| Role                              | Policy                                   | Purpose                                                       |
|------------------------------------|---------------------------------------------|--------------------------------------------------------------------|
| `ec2-role-sqlserver-backup`        | `AmazonSSMManagedInstanceCore` (managed)     | Allow the SSM Agent to receive/run Run Commands                      |
| `ec2-role-sqlserver-backup`        | `AllowS3BackupUpload` (inline)               | Allow `aws s3 cp` of the `.bak` file to the destination bucket        |
| `lambda-role-ec2-backup`           | `AWSLambdaBasicExecutionRole` (managed)      | CloudWatch logging                                                    |
| `lambda-role-ec2-backup`           | `AllowSSMSendCommand` (inline)               | Trigger and track the Run Command on the EC2 instance                 |

---

## 4. Security notes

- **Principle of least privilege**: the inline policies above already
  restrict S3 to a specific bucket and SSM to a specific instance +
  document. Adjust `<bucket-name>`, `<region>`, `<account-id>`, and
  `<instance-id>` to match your environment.
- **Multiple instances**: if there's more than one SQL Server box (e.g.
  production and staging), list all instance ARNs in the `Resource` array
  of `ssm:SendCommand`, or use tags + `aws:ResourceTag` in a condition to
  simplify management.
- **EventBridge -> Lambda**: the `lambda:AddPermission` permission (done via
  `aws lambda add-permission` in the main README) is separate from the
  roles above — it's a resource policy on the Lambda itself, not an IAM Role.
