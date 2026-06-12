<#
.SYNOPSIS
    Faz backup nativo (FULL ou DIFFERENTIAL) de todos os bancos de usuario
    de uma instancia SQL Server local (EC2) e envia o arquivo para o S3.

.DESCRIPTION
    Este script e pensado para ser executado DENTRO da instancia EC2
    (Windows) via AWS Systems Manager Run Command (AWS-RunPowerShellScript),
    disparado por uma Lambda agendada pelo EventBridge.

    Parametros sao recebidos como variaveis de ambiente (definidas pelo
    SSM Run Command a partir do payload da Lambda):

      BACKUP_TYPE   -> FULL ou DIFFERENTIAL          (default: FULL)
      S3_BUCKET     -> nome do bucket S3
      S3_PREFIX     -> prefixo dentro do bucket       (ex: backups/full/)
      LOCAL_DIR     -> diretorio local temporario     (default: C:\SQLBackups)
      SQL_INSTANCE  -> nome da instancia SQL Server   (default: localhost)
      RETAIN_LOCAL  -> "true"/"false" mantem copia local apos upload (default: false)

.NOTES
    - Requer modulo SqlServer (Install-Module SqlServer) OU sqlcmd no PATH.
    - Requer AWS CLI configurado (ou IAM Role da instancia com permissao S3).
    - A instancia EC2 precisa de uma IAM Role com permissao de escrita no bucket S3.
#>

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# 1. Leitura de parametros (vindos do SSM Run Command / Lambda)
# ---------------------------------------------------------------------------
$BackupType  = $env:BACKUP_TYPE
if (-not $BackupType) { $BackupType = "FULL" }
$BackupType  = $BackupType.ToUpper()

$S3Bucket    = $env:S3_BUCKET
$S3Prefix    = $env:S3_PREFIX
if (-not $S3Prefix) { $S3Prefix = "" }

$LocalDir    = $env:LOCAL_DIR
if (-not $LocalDir) { $LocalDir = "C:\SQLBackups" }

$SqlInstance = $env:SQL_INSTANCE
if (-not $SqlInstance) { $SqlInstance = "localhost" }

$RetainLocal = $env:RETAIN_LOCAL
if (-not $RetainLocal) { $RetainLocal = "false" }

if (-not $S3Bucket) {
    throw "S3_BUCKET nao informado."
}

if ($BackupType -notin @("FULL", "DIFFERENTIAL")) {
    throw "BACKUP_TYPE invalido: $BackupType. Use FULL ou DIFFERENTIAL."
}

# Tipo SQL Server: FULL = DATABASE, DIFFERENTIAL = DATABASE ... WITH DIFFERENTIAL
$FileExt = if ($BackupType -eq "FULL") { "bak" } else { "diff.bak" }

# ---------------------------------------------------------------------------
# 2. Preparacao
# ---------------------------------------------------------------------------
if (-not (Test-Path $LocalDir)) {
    New-Item -ItemType Directory -Path $LocalDir -Force | Out-Null
}

$Timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddHHmmss")

Import-Module SqlServer -ErrorAction SilentlyContinue

# Lista bancos de usuario (exclui bancos de sistema)
$query = @"
SELECT name
FROM sys.databases
WHERE name NOT IN ('master','tempdb','model','msdb')
  AND state_desc = 'ONLINE'
"@

$databases = Invoke-Sqlcmd -ServerInstance $SqlInstance -Query $query -ErrorAction Stop |
    Select-Object -ExpandProperty name

if (-not $databases) {
    Write-Output "Nenhum banco de usuario encontrado para backup."
    exit 0
}

$results = @()

# ---------------------------------------------------------------------------
# 3. Backup de cada banco
# ---------------------------------------------------------------------------
foreach ($db in $databases) {

    $fileName = "${db}_${BackupType}_${Timestamp}.${FileExt}"
    $localPath = Join-Path $LocalDir $fileName

    try {
        if ($BackupType -eq "FULL") {
            $sql = @"
BACKUP DATABASE [$db]
TO DISK = N'$localPath'
WITH INIT, COMPRESSION, CHECKSUM, STATS = 10;
"@
        }
        else {
            # DIFFERENTIAL exige FULL previo do mesmo banco
            $checkFull = @"
SELECT TOP 1 1
FROM msdb.dbo.backupset
WHERE database_name = '$db' AND type = 'D'
ORDER BY backup_finish_date DESC
"@
            $hasFull = Invoke-Sqlcmd -ServerInstance $SqlInstance -Query $checkFull -ErrorAction Stop
            if (-not $hasFull) {
                $results += "[$BackupType][SKIP] $db -> Nenhum FULL backup encontrado, execute FULL primeiro."
                continue
            }

            $sql = @"
BACKUP DATABASE [$db]
TO DISK = N'$localPath'
WITH DIFFERENTIAL, INIT, COMPRESSION, CHECKSUM, STATS = 10;
"@
        }

        Write-Output "Executando backup $BackupType para banco '$db'..."
        Invoke-Sqlcmd -ServerInstance $SqlInstance -Query $sql -QueryTimeout 0 -ErrorAction Stop

        if (-not (Test-Path $localPath)) {
            throw "Arquivo de backup nao foi criado em $localPath"
        }

        # -------------------------------------------------------------
        # 4. Upload para o S3
        # -------------------------------------------------------------
        $s3Key = "${S3Prefix}${fileName}"
        $s3Uri = "s3://$S3Bucket/$s3Key"

        Write-Output "Enviando para $s3Uri ..."
        aws s3 cp $localPath $s3Uri --only-show-errors
        if ($LASTEXITCODE -ne 0) {
            throw "Falha ao enviar $localPath para $s3Uri (exit code $LASTEXITCODE)"
        }

        $results += "[$BackupType][OK] $db -> $s3Uri"

        # -------------------------------------------------------------
        # 5. Limpeza local (opcional)
        # -------------------------------------------------------------
        if ($RetainLocal.ToLower() -ne "true") {
            Remove-Item -Path $localPath -Force -ErrorAction SilentlyContinue
        }
    }
    catch {
        $results += "[$BackupType][ERROR] $db -> $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------------------
# 6. Saida final (sera capturada pelo SSM e lida pela Lambda)
# ---------------------------------------------------------------------------
$results | ForEach-Object { Write-Output $_ }

if ($results | Where-Object { $_ -like "*ERROR*" }) {
    exit 1
} else {
    exit 0
}
