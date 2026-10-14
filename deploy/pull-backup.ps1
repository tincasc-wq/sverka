<#
Копия ночного бэкапа базы с сервера на этот компьютер — на случай потери самого сервера.

  .\deploy\pull-backup.ps1            # скачать свежий дамп, если его здесь ещё нет
  .\deploy\pull-backup.ps1 -Install   # один раз: задача Windows «Сверка — копия бэкапа»
                                      # (ежедневно в 10:00 и при входе в систему)

Сервер делает дамп в 03:30 МСК и хранит 14 дней. Здесь — папка backups\ в проекте
(в git не попадает: *.dump), тоже 14 последних копий. Журнал — backups\pull.log.
#>
param([switch]$Install)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Dir = Join-Path $Root "backups"
$Log = Join-Path $Dir "pull.log"
$Key = Join-Path $HOME ".ssh\sverka_deploy"
$ServerFile = Join-Path $Root ".deploy-server"
$Keep = 14

if ($Install) {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`""
    $triggers = @(
        (New-ScheduledTaskTrigger -Daily -At 10:00),
        (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME)
    )
    # Ноутбук ночью выключен: пропущенный запуск выполнить, как только он включится.
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    Register-ScheduledTask -TaskName "Сверка — копия бэкапа" -Action $action -Trigger $triggers `
        -Settings $settings -Description "Копирует ночной дамп базы ang-rist.ru в $Dir" -Force | Out-Null
    Write-Host "Задача «Сверка — копия бэкапа» установлена: ежедневно в 10:00 и при входе в систему."
    exit 0
}

New-Item -ItemType Directory -Force $Dir | Out-Null
function Write-Log([string]$Text) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Text"
    Add-Content -Path $Log -Value $line -Encoding utf8
    Write-Host $line
}

try {
    $Server = (Get-Content $ServerFile -ErrorAction Stop | Select-Object -First 1).Trim()
    $Target = "sverka@$Server"
    $SshOpts = @("-i", $Key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20")

    $latest = (& ssh @SshOpts $Target "ls -1t ~/backups/sverka-*.dump 2>/dev/null | head -1" 2>$null)
    if (-not $latest) { throw "на сервере нет дампов (или нет связи)" }
    $latest = $latest.Trim()
    $name = Split-Path $latest -Leaf
    $local = Join-Path $Dir $name
    $remoteSize = [long]((& ssh @SshOpts $Target "stat -c %s '$latest'" 2>$null).Trim())

    if ((Test-Path $local) -and (Get-Item $local).Length -eq $remoteSize) {
        Write-Log "уже есть: $name"
    } else {
        $part = "$local.part"
        & scp -q @SshOpts "${Target}:$latest" $part 2>$null
        if ($LASTEXITCODE -ne 0) { throw "scp завершился с кодом $LASTEXITCODE" }
        if ((Get-Item $part).Length -ne $remoteSize) { throw "размер не совпал с серверным — копия неполная" }
        Move-Item -Force $part $local
        Write-Log ("скачан {0} ({1:N0} МБ)" -f $name, ($remoteSize / 1MB))
    }

    Get-ChildItem $Dir -Filter "sverka-*.dump" | Sort-Object Name -Descending |
        Select-Object -Skip $Keep | ForEach-Object { Remove-Item $_.FullName; Write-Log "удалён старый: $($_.Name)" }
} catch {
    Write-Log "ОШИБКА: $($_.Exception.Message)"
    exit 1
}
