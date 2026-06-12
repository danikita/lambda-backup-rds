<#
.SYNOPSIS
    Performs a native (FULL or DIFFERENTIAL) backup of all user databases
    on a local SQL Server instance (EC2) and uploads the file to S3.

.DESCRIPTION
    This script is intended to run INSIDE the EC2 instance (Windows) via
    AWS Systems Manager Run Command (AWS-RunPowerShellScript), triggered
    by a Lambda function scheduled through EventBridge.

    Parameters are received as environment variables (set by SSM Run
    Command from the Lambda payload):

      BACKUP_TYPE   -> FULL or DIFFERENTIAL          (default: FULL)
      S3_BUCKET     -> destination S3 bucket name
      S3_PREFIX     -> prefix inside the bucket       (e.g. backups/full/)
      LOCAL_DIR     -> local temporary directory      (default: C:\SQLBackups)
      SQL_INSTANCE  -> SQL Server instance name       (default: localhost)
      RETAIN_LOCAL  -> "true"/"false" keep local copy after upload (default: false)

.NOTES
    - Requires the SqlServer module (Install-Module SqlServer) OR sqlcmd in PATH.
    - Requires AWS CLI configured (or an EC2 instance IAM Role with S3 permissions).
    - The EC2 instance needs an IAM Role with write permission to the S3 bucket.
#>

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# 1. Read parameters (from SSM Run Command / Lambda)
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
    throw "S3_BUCKET was not provided."
}

if ($BackupType -notin @("FULL", "DIFFERENTIAL")) {
    throw "Invalid BACKUP_TYPE: $BackupType. Use FULL or DIFFERENTIAL."
}

# SQL Server type: FULL = DATABASE, DIFFERENTIAL = DATABASE ... WITH DIFFERENTIAL
$FileExt = if ($BackupType -eq "FULL") { "bak" } else { "diff.bak" }

# ---------------------------------------------------------------------------
# 2. Preparation
# ---------------------------------------------------------------------------
if (-not (Test-Path $LocalDir)) {
    New-Item -ItemType Directory -Path $LocalDir -Force | Out-Null
}

$Timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddHHmmss")

Import-Module SqlServer -ErrorAction SilentlyContinue

# List user databases (excludes system databases)
$query = @"
SELECT name
FROM sys.databases
WHERE name NOT IN ('master','tempdb','model','msdb')
  AND state_desc = 'ONLINE'
"@

$databases = Invoke-Sqlcmd -ServerInstance $SqlInstance -Query $query -ErrorAction Stop |
    Select-Object -ExpandProperty name

if (-not $databases) {
    Write-Output "No user databases found to back up."
    exit 0
}

$results = @()

# ---------------------------------------------------------------------------
# 3. Backup each database
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
            # DIFFERENTIAL requires a prior FULL backup of the same database
            $checkFull = @"
SELECT TOP 1 1
FROM msdb.dbo.backupset
WHERE database_name = '$db' AND type = 'D'
ORDER BY backup_finish_date DESC
"@
            $hasFull = Invoke-Sqlcmd -ServerInstance $SqlInstance -Query $checkFull -ErrorAction Stop
            if (-not $hasFull) {
                $results += "[$BackupType][SKIP] $db -> No FULL backup found, run a FULL backup first."
                continue
            }

            $sql = @"
BACKUP DATABASE [$db]
TO DISK = N'$localPath'
WITH DIFFERENTIAL, INIT, COMPRESSION, CHECKSUM, STATS = 10;
"@
        }

        Write-Output "Running $BackupType backup for database '$db'..."
        Invoke-Sqlcmd -ServerInstance $SqlInstance -Query $sql -QueryTimeout 0 -ErrorAction Stop

        if (-not (Test-Path $localPath)) {
            throw "Backup file was not created at $localPath"
        }

        # -------------------------------------------------------------
        # 4. Upload to S3
        # -------------------------------------------------------------
        $s3Key = "${S3Prefix}${fileName}"
        $s3Uri = "s3://$S3Bucket/$s3Key"

        Write-Output "Uploading to $s3Uri ..."
        aws s3 cp $localPath $s3Uri --only-show-errors
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to upload $localPath to $s3Uri (exit code $LASTEXITCODE)"
        }

        $results += "[$BackupType][OK] $db -> $s3Uri"

        # -------------------------------------------------------------
        # 5. Local cleanup (optional)
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
# 6. Final output (captured by SSM and read by the Lambda)
# ---------------------------------------------------------------------------
$results | ForEach-Object { Write-Output $_ }

if ($results | Where-Object { $_ -like "*ERROR*" }) {
    exit 1
} else {
    exit 0
}
