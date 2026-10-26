[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$localDirectory = Join-Path $projectRoot '.local'
New-Item -ItemType Directory -Path $localDirectory -Force | Out-Null

$ports = [ordered]@{ 'env-test' = 5445; 'env-qa' = 5446; 'main' = 5447 }
foreach ($environment in $ports.Keys) {
    $configurationPath = Join-Path $localDirectory "$environment.env"
    if (Test-Path -LiteralPath $configurationPath) {
        Write-Host "Zachowano istniejaca konfiguracje: $environment"
        continue
    }
    $randomBytes = New-Object byte[] 32
    $randomGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $randomGenerator.GetBytes($randomBytes) } finally { $randomGenerator.Dispose() }
    $databasePassword = [Convert]::ToBase64String($randomBytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    @(
        'POSTGRES_USER=psip'
        'POSTGRES_DB=psip'
        "POSTGRES_PORT=$($ports[$environment])"
        "POSTGRES_PASSWORD=$databasePassword"
    ) | Set-Content -LiteralPath $configurationPath -Encoding ascii
    Write-Host "Utworzono konfiguracje: $environment, port $($ports[$environment])"
    $databasePassword = $null
}
