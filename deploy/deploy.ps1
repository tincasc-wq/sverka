<#
Развёртывание на сервер с Windows. Ключ: $HOME\.ssh\sverka_deploy (создан 13.10.2026).

  .\deploy\deploy.ps1 setup -Server 1.2.3.4   # один раз: настройка чистого VPS (от root)
  .\deploy\deploy.ps1 env                      # загрузить .env.prod → ~/sverka/.env
  .\deploy\deploy.ps1 push                     # выложить текущий коммит и пересобрать
  .\deploy\deploy.ps1 db                       # перенести базу с локальной машины
  .\deploy\deploy.ps1 model                    # перенести модель эмбеддингов (если HF недоступен)
  .\deploy\deploy.ps1 images                   # перенести базовые образы (если Docker Hub недоступен)
  .\deploy\deploy.ps1 ps | logs [сервис] | backup | ssh

Адрес сервера запоминается в .deploy-server после первого -Server.
#>
param(
    [Parameter(Position = 0)][string]$Command = "help",
    [Parameter(Position = 1)][string]$Arg = "",
    [string]$Server = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Key = Join-Path $HOME ".ssh\sverka_deploy"
$ServerFile = Join-Path $Root ".deploy-server"
$Compose = "docker compose -f deploy/docker-compose.prod.yml"
$Distro = "Ubuntu"
$WslRoot = "/mnt/c/Project/sverka"

if ($Server) { Set-Content -Path $ServerFile -Value $Server -NoNewline }
elseif (Test-Path $ServerFile) { $Server = (Get-Content $ServerFile -Raw).Trim() }
if (-not $Server -and $Command -ne "help") { throw "Укажите адрес: -Server <IP или домен>" }

$SshOpts = @("-i", $Key, "-o", "StrictHostKeyChecking=accept-new")

function Remote([string]$User, [string]$Cmd) {
    ssh @SshOpts "$User@$Server" $Cmd
    if ($LASTEXITCODE -ne 0) { throw "Команда на сервере завершилась с ошибкой ($LASTEXITCODE)" }
}

function Upload([string]$User, [string]$Local, [string]$RemotePath) {
    scp @SshOpts $Local "${User}@${Server}:$RemotePath"
    if ($LASTEXITCODE -ne 0) { throw "Не удалось загрузить $Local" }
}

function App([string]$Cmd) { Remote "sverka" "cd ~/sverka && $Cmd" }

function Wsl([string]$Cmd) {
    wsl -d $Distro -u root -- bash -c "cd $WslRoot && $Cmd"
    if ($LASTEXITCODE -ne 0) { throw "Локальная команда завершилась с ошибкой ($LASTEXITCODE)" }
}

switch ($Command) {
    "setup" {
        Upload "root" (Join-Path $PSScriptRoot "server-setup.sh") "/root/server-setup.sh"
        Remote "root" "sed -i 's/\r$//' /root/server-setup.sh && bash /root/server-setup.sh"
    }
    "env" {
        $envFile = Join-Path $Root ".env.prod"
        if (-not (Test-Path $envFile)) { throw "Нет .env.prod — заполните по образцу deploy\env.prod.example" }
        Remote "sverka" "mkdir -p ~/sverka"
        Upload "sverka" $envFile "sverka/.env"
        Remote "sverka" "chmod 600 ~/sverka/.env"
        Write-Host "Секреты загружены. Перезапуск с ними: .\deploy\deploy.ps1 push"
    }
    "push" {
        Push-Location $Root
        try {
            $dirty = git status --porcelain
            if ($dirty) { Write-Warning "Есть незакоммиченные изменения — на сервер уйдёт последний коммит." }
            $tar = Join-Path $env:TEMP "sverka.tar"
            git archive --format=tar -o $tar HEAD
            if ($LASTEXITCODE -ne 0) { throw "git archive не удался" }
        } finally { Pop-Location }
        Remote "sverka" "mkdir -p ~/sverka"
        Upload "sverka" $tar "/tmp/sverka.tar"
        Remove-Item $tar
        App ("tar -xf /tmp/sverka.tar && rm /tmp/sverka.tar && sed -i 's/\r$//' deploy/*.sh " +
             "&& chmod +x deploy/*.sh && $Compose up -d --build --remove-orphans && $Compose ps")
        Write-Host "Готово. Не забудьте остановить локального бота: wsl ... docker compose stop bot"
    }
    "db" {
        Write-Host "Снимаю дамп локальной базы…"
        Wsl "docker compose exec -T db pg_dump -U sverka -Fc sverka > sverka.dump"
        Upload "sverka" (Join-Path $Root "sverka.dump") "/tmp/sverka.dump"
        Remove-Item (Join-Path $Root "sverka.dump")
        App ("$Compose stop app bot worker && " +
             "$Compose exec -T db pg_restore -U sverka -d sverka --clean --if-exists --no-owner < /tmp/sverka.dump; " +
             "rm /tmp/sverka.dump && $Compose start app bot worker && $Compose ps")
    }
    "model" {
        Write-Host "Упаковываю модель эмбеддингов (~2.3 ГБ)…"
        Wsl "docker run --rm -v sverka_hf_cache:/data alpine tar -C /data -cf - . > hf_cache.tar"
        Upload "sverka" (Join-Path $Root "hf_cache.tar") "/tmp/hf_cache.tar"
        Remove-Item (Join-Path $Root "hf_cache.tar")
        App ("docker volume create sverka_hf_cache >/dev/null && " +
             "docker run --rm -i -v sverka_hf_cache:/data alpine tar -C /data -xf - < /tmp/hf_cache.tar && " +
             "rm /tmp/hf_cache.tar && $Compose restart worker")
    }
    "images" {
        $images = "pgvector/pgvector:pg16 redis:7-alpine caddy:2-alpine python:3.12-slim alpine:latest"
        Write-Host "Сохраняю базовые образы…"
        Wsl "for i in $images; do docker pull -q `$i >/dev/null || true; done; docker save $images > images.tar"
        Upload "sverka" (Join-Path $Root "images.tar") "/tmp/images.tar"
        Remove-Item (Join-Path $Root "images.tar")
        App "docker load < /tmp/images.tar && rm /tmp/images.tar"
    }
    "ps" { App "$Compose ps" }
    "logs" { App "$Compose logs --tail 100 $Arg" }
    "backup" { App "deploy/backup.sh && ls -lh ~/backups | tail -5" }
    "ssh" { ssh @SshOpts "sverka@$Server" }
    default { Get-Help $PSCommandPath; Write-Host (Get-Content $PSCommandPath -TotalCount 15 | Out-String) }
}
