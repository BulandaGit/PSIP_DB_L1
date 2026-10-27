[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SourceDirectory,
    [string]$OutputDirectory,
    [switch]$SkipImport
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$sourcePath = (Resolve-Path -LiteralPath $SourceDirectory).Path
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $projectRoot '.local/bdot-reports' }
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$reportPath = (Resolve-Path -LiteralPath $OutputDirectory).Path
$configuration = Join-Path $projectRoot '.local/env-test.env'
if (-not (Test-Path -LiteralPath $configuration)) { throw 'Brakuje konfiguracji .local/env-test.env' }
$composeFile = Join-Path $projectRoot 'docker_compose.yml'
$composeArguments = @('compose', '--env-file', $configuration, '-f', $composeFile,
                      '-p', 'remote-db-env-test', '--profile', 'tools')

& docker @composeArguments build python
if ($LASTEXITCODE -ne 0) { throw 'Nie udalo sie zbudowac narzedzi Python.' }
& docker @composeArguments up -d --wait postgis
if ($LASTEXITCODE -ne 0) { throw 'Baza env-test nie jest gotowa.' }
if (-not $SkipImport) {
    & docker @composeArguments run --rm --no-deps -T --volume "${sourcePath}:/source:ro" --volume "${reportPath}:/results" `
        python python /app/python/bdot_import.py --source /source --county-code 0602 --year 2025 `
        --report /results/BDOT_0602_import.json
    if ($LASTEXITCODE -ne 0) { throw 'Import BDOT10K nie powiodl sie. Transakcja zostala wycofana.' }
}
& docker @composeArguments run --rm --no-deps -T --volume "${reportPath}:/results" `
    python python /app/python/bdot_aggregate.py --county-code 0602 --resolutions 6 7 8 9 `
    --report /results/BDOT_0602_agregacja.json --csv /results/BDOT_0602_kontrola.csv
if ($LASTEXITCODE -ne 0) { throw 'Agregacja nie powiodla sie. Transakcja zostala wycofana.' }
Write-Host "Gotowe. Obliczenia EPSG:2180, geometrie wynikowe EPSG:3857. Raporty: $reportPath"
