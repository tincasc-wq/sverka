<#
Обёртка над docker compose в WSL Ubuntu, чтобы работать из PowerShell в Windows.

  .\dev.ps1 up        # поднять db, redis, app, worker (и не дать WSL уснуть)
  .\dev.ps1 down      # остановить
  .\dev.ps1 test      # pytest в контейнере
  .\dev.ps1 lint      # ruff check + format --check
  .\dev.ps1 fmt       # ruff format
  .\dev.ps1 migrate   # alembic upgrade head
  .\dev.ps1 revision "описание"   # новая миграция (autogenerate)
  .\dev.ps1 logs [сервис]
  .\dev.ps1 sh        # shell в контейнере app
#>
param(
    [Parameter(Position = 0)][string]$Command = "help",
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Rest
)

$ErrorActionPreference = "Stop"
$Distro = "Ubuntu"
$ProjectPath = "/mnt/c/Project/sverka"
# Порт локального прокси Hiddify («Поделиться VPN в локальной сети»); 0 — без прокси.
$TelegramProxyPort = 12334

function Invoke-Compose([string]$ComposeArgs) {
    wsl -d $Distro -u root -- bash -c "cd $ProjectPath && docker compose $ComposeArgs"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

function Start-WslKeepAlive {
    # WSL останавливается, когда нет открытых сессий, и контейнеры гаснут вместе с ним.
    $alive = Get-CimInstance Win32_Process -Filter "Name = 'wsl.exe'" |
        Where-Object { $_.CommandLine -like "*sleep infinity*" }
    if (-not $alive) {
        Start-Process wsl -ArgumentList "-d $Distro -u root -- sleep infinity" -WindowStyle Hidden
    }
}

switch ($Command) {
    "up" {
        Start-WslKeepAlive
        if ($TelegramProxyPort -gt 0) {
            # Адрес Windows в сети WSL меняется после перезагрузки — определяем при каждом запуске.
            $proxy = "TELEGRAM_PROXY=http://`$(ip route show default | awk '{print `$3}'):$TelegramProxyPort"
            wsl -d $Distro -u root -- bash -c "cd $ProjectPath && $proxy docker compose up -d --build"
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        } else {
            Invoke-Compose "up -d --build"
        }
        Write-Host "Готово: http://localhost:8000/health"
    }
    "down" { Invoke-Compose "down" }
    "test" { Invoke-Compose "exec -T app pytest -q $($Rest -join ' ')" }
    "lint" {
        Invoke-Compose "exec -T app ruff check ."
        Invoke-Compose "exec -T app ruff format --check ."
    }
    "fmt" { Invoke-Compose "exec -T app ruff format ." }
    "migrate" { Invoke-Compose "exec -T app alembic upgrade head" }
    "revision" { Invoke-Compose "exec -T app alembic revision --autogenerate -m '$($Rest -join ' ')'" }
    "logs" { Invoke-Compose "logs --tail 100 $($Rest -join ' ')" }
    "sh" { wsl -d $Distro -u root -- bash -c "cd $ProjectPath && docker compose exec app bash" }
    default { Get-Content $PSCommandPath -Encoding UTF8 -TotalCount 13 | Select-Object -Skip 1 }
}
