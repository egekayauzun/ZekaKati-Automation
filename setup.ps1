$ErrorActionPreference = "Stop"

Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

if (-not (Test-Path ".\venv")) {
    Write-Host "Sanal ortam bulunamadi, olusturuluyor..."
    python -m venv venv
}

Write-Host "Sanal ortam aktif ediliyor..."
. .\venv\Scripts\Activate.ps1

Write-Host "Gerekli kutuphaneler yukleniyor..."
pip install -r requirements.txt

$directories = @(
    ".\assets\audio",
    ".\assets\images",
    ".\assets\videos",
    ".\assets\music",
    ".\assets\output",
    ".\logs"
)

foreach ($dir in $directories) {
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
}

if (-not (Test-Path ".\.env")) {
    Write-Warning "Lütfen .env dosyasını oluşturun"
}

Write-Host "Ortam başarıyla hazırlandı!"
