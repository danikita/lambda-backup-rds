# Politicas IAM Necessarias

Este projeto envolve duas IAM Roles distintas: a **Role da instancia EC2**
(onde o SQL Server roda) e a **Role da Lambda** (que dispara o SSM Run Command).

---

## 1. IAM Role da instancia EC2

A instancia precisa de um **Instance Profile** com a seguinte role anexada.

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

### 1.2. Politica gerenciada — SSM

Necessaria para o SSM Agent se comunicar com o Systems Manager e receber
o Run Command:

```bash
aws iam attach-role-policy \
  --role-name ec2-role-sqlserver-backup \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
```

### 1.3. Politica inline — acesso ao S3 (upload dos backups)

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

### 1.4. Criar e associar o Instance Profile

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

> Se a instancia ja existir sem instance profile, use `associate-iam-instance-profile`
> como acima. Se ja tiver um profile, use `replace-iam-instance-profile-association`.

---

## 2. IAM Role da Lambda

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

### 2.2. Politica gerenciada — execucao basica (CloudWatch Logs)

```bash
aws iam attach-role-policy \
  --role-name lambda-role-ec2-backup \
  --policy-arn arn:aws:iam::aws:policy/AWSLambdaBasicExecutionRole
```

### 2.3. Politica inline — SSM Run Command

A Lambda precisa enviar o comando e consultar o resultado da execucao.
Abaixo, a versao **restrita** (recomendada), limitando a acao apenas a
instancia especifica e ao documento `AWS-RunPowerShellScript`:

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

> `ssm:GetCommandInvocation` nao suporta restricao por ARN de recurso
> especifico, por isso o `Resource: "*"` nessa acao. Caso queira reduzir
> ainda mais o escopo, restrinja por tags/condicoes no nivel do `ssm:SendCommand`.

#### Versao simplificada (menos restritiva)

Se preferir liberar para qualquer instancia/documento (uteis em ambientes
de teste, mas evite em producao):

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

## 3. Resumo das permissoes

| Role                              | Politica                                | Finalidade                                              |
|------------------------------------|--------------------------------------------|------------------------------------------------------------|
| `ec2-role-sqlserver-backup`        | `AmazonSSMManagedInstanceCore` (gerenciada) | Permitir que o SSM Agent receba/execute Run Commands         |
| `ec2-role-sqlserver-backup`        | `AllowS3BackupUpload` (inline)              | Permitir `aws s3 cp` do `.bak` para o bucket de destino       |
| `lambda-role-ec2-backup`           | `AWSLambdaBasicExecutionRole` (gerenciada)  | Logs no CloudWatch                                            |
| `lambda-role-ec2-backup`           | `AllowSSMSendCommand` (inline)              | Disparar e acompanhar o Run Command na EC2                    |

---

## 4. Observacoes de seguranca

- **Principio do menor privilegio**: as politicas inline acima ja restringem
  o S3 a um bucket especifico e a SSM a uma instancia + documento especificos.
  Ajuste `<bucket-name>`, `<region>`, `<account-id>` e `<instance-id>` conforme
  seu ambiente.
- **Multiplas instancias**: se houver mais de um servidor SQL Server (ex:
  produção e homologação), liste todos os ARNs de instancia no array
  `Resource` do `ssm:SendCommand`, ou use tags + `aws:ResourceTag` em uma
  condition para simplificar.
- **EventBridge -> Lambda**: a permissao `lambda:AddPermission` (feita via
  `aws lambda add-permission` no README principal) e separada das roles
  acima — ela e uma resource policy da própria Lambda, nao uma IAM Role.
