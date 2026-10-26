[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$repository = 'BulandaGit/PSIP_DB_L1'
$runnerCompose = Join-Path $projectRoot 'runner-compose.yml'
$statusPath = Join-Path $projectRoot '.local/setup-status.json'
$originalGitHubToken = $env:GH_TOKEN
$originalGitHubConfig = $env:GH_CONFIG_DIR
$originalPromptSetting = $env:GH_PROMPT_DISABLED
$tokenPointer = [IntPtr]::Zero
$exitStatus = 1

function Invoke-CheckedGh {
    param([string[]]$Arguments)
    & $script:githubCli @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "GitHub CLI: blad przy operacji $($Arguments[0]) $($Arguments[1])." }
}

try {
    Set-Location -LiteralPath $projectRoot
    Write-Host 'Konfiguracja cwiczenia PSIP nr 2' -ForegroundColor Cyan
    Write-Host 'Token wpisujesz tylko w tym oknie. Nie zostanie zapisany na dysku.'

    & docker info --format '{{.ServerVersion}}' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Uruchom Docker Desktop i sprobuj ponownie.' }
    & (Join-Path $PSScriptRoot 'New-LocalConfiguration.ps1')

    $toolsDirectory = Join-Path $projectRoot '.tools'
    $cliDirectory = Join-Path $toolsDirectory 'gh_2.103.2_windows_amd64'
    $script:githubCli = Join-Path $cliDirectory 'bin/gh.exe'
    if (-not (Test-Path -LiteralPath $script:githubCli)) {
        Write-Host 'Pobieram GitHub CLI z oficjalnego repozytorium i sprawdzam jego sume SHA256...'
        New-Item -ItemType Directory -Path $toolsDirectory -Force | Out-Null
        $archivePath = Join-Path $toolsDirectory 'gh_2.103.2_windows_amd64.zip'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/cli/cli/releases/download/v2.103.2/gh_2.103.2_windows_amd64.zip' -OutFile $archivePath
        $expectedHash = 'fef9fcdd8fdabd0501ab8b3d99eac976f504ab55d01bef9e8ef428a5d900ddda'
        if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) {
            throw 'Nieprawidlowa suma kontrolna GitHub CLI. Przerwano konfiguracje.'
        }
        Expand-Archive -LiteralPath $archivePath -DestinationPath $cliDirectory -Force
    }

    Write-Host ''
    $secureToken = Read-Host 'Wklej token GitHub classic (uprawnienie repo) i nacisnij Enter' -AsSecureString
    $tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
    $env:GH_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
    if ([string]::IsNullOrWhiteSpace($env:GH_TOKEN)) { throw 'Nie podano tokena.' }
    $env:GH_CONFIG_DIR = Join-Path $projectRoot '.local/gh-config'
    $env:GH_PROMPT_DISABLED = '1'

    $authenticatedUser = & $script:githubCli api user --jq .login
    if ($LASTEXITCODE -ne 0) { throw 'Token jest nieprawidlowy lub wygasl.' }
    if ($authenticatedUser.Trim() -ne 'BulandaGit') { throw 'Token musi pochodzic z konta BulandaGit.' }

    $existingEnvironmentsJson = & $script:githubCli api "repos/$repository/environments"
    if ($LASTEXITCODE -ne 0) { throw 'Nie mozna odczytac srodowisk GitHub. Sprawdz uprawnienie repo.' }
    $existingEnvironmentNames = @(($existingEnvironmentsJson | ConvertFrom-Json).environments | ForEach-Object { $_.name })
    foreach ($environment in @('env-test', 'env-qa', 'main')) {
        Write-Host "Konfiguruje srodowisko GitHub: $environment"
        if ($existingEnvironmentNames -notcontains $environment) {
            Invoke-CheckedGh -Arguments @('api', '--method', 'PUT', "repos/$repository/environments/$environment")
        }
        $values = @{}
        Get-Content -LiteralPath (Join-Path $projectRoot ".local/$environment.env") | ForEach-Object {
            $parts = $_ -split '=', 2
            if ($parts.Count -eq 2) { $values[$parts[0]] = $parts[1] }
        }
        foreach ($variable in @('POSTGRES_USER', 'POSTGRES_DB', 'POSTGRES_PORT')) {
            Invoke-CheckedGh -Arguments @('variable', 'set', $variable, '--repo', $repository, '--env', $environment, '--body', $values[$variable])
        }
        $values['POSTGRES_PASSWORD'] | & $script:githubCli secret set POSTGRES_PASSWORD --repo $repository --env $environment
        if ($LASTEXITCODE -ne 0) { throw "Nie udalo sie zapisac sekretu dla $environment." }
        $values.Clear()
    }

    Write-Host 'Przygotowuje kontener runnera...'
    & docker compose -p psip-db-runner -f $runnerCompose build runner
    if ($LASTEXITCODE -ne 0) { throw 'Nie udalo sie zbudowac runnera.' }
    & docker compose -p psip-db-runner -f $runnerCompose run --rm -T runner registered
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Rejestruje runnera krotkotrwalym tokenem. Token classic nie trafia do kontenera.'
        $registrationToken = & $script:githubCli api --method POST "repos/$repository/actions/runners/registration-token" --jq .token
        if ($LASTEXITCODE -ne 0) { throw 'Nie udalo sie pobrac tokena rejestracji runnera. Sprawdz uprawnienie repo.' }
        $registrationToken | & docker compose -p psip-db-runner -f $runnerCompose run --rm -T runner register
        $registrationToken = $null
        if ($LASTEXITCODE -ne 0) { throw 'Nie udalo sie zarejestrowac runnera.' }
    }
    & docker compose -p psip-db-runner -f $runnerCompose up -d runner
    if ($LASTEXITCODE -ne 0) { throw 'Nie udalo sie uruchomic runnera.' }

    Write-Host 'Czekam, az GitHub potwierdzi polaczenie runnera...'
    $runnerOnline = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $runnerResponse = & $script:githubCli api "repos/$repository/actions/runners"
        if ($LASTEXITCODE -ne 0) { throw 'Nie mozna odczytac statusu runnera.' }
        $runners = $runnerResponse | ConvertFrom-Json
        $runner = $runners.runners | Where-Object { $_.name -eq 'psip-local-docker' }
        if ($runner.status -eq 'online') { $runnerOnline = $true; break }
        Start-Sleep -Seconds 2
    }
    if (-not $runnerOnline) { throw 'Runner nie uzyskal statusu online. Sprawdz jego logi.' }

    @{ success = $true; repository = $repository; runner = 'online'; completedAt = (Get-Date).ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding UTF8
    Write-Host ''
    Write-Host 'GOTOWE: trzy srodowiska skonfigurowane, runner online.' -ForegroundColor Green
    Write-Host 'Wroc do czatu. Nastepny etap: uruchomienie GitHub Actions i pull request.'
    $exitStatus = 0
}
catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    if (Test-Path -LiteralPath (Split-Path $statusPath -Parent)) {
        @{ success = $false; message = $_.Exception.Message; completedAt = (Get-Date).ToString('o') } |
            ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding UTF8
    }
}
finally {
    $env:GH_TOKEN = $originalGitHubToken
    $env:GH_CONFIG_DIR = $originalGitHubConfig
    $env:GH_PROMPT_DISABLED = $originalPromptSetting
    if ($tokenPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer) }
    if ($secureToken) { $secureToken.Dispose() }
    $registrationToken = $null
}

[void](Read-Host 'Nacisnij Enter, aby zamknac okno')
exit $exitStatus
