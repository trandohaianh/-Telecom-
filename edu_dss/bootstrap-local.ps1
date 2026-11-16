$ErrorActionPreference = 'Stop'
$secretDir = Join-Path $PSScriptRoot 'secrets'
$tlsDir = Join-Path $secretDir 'tls'
New-Item -ItemType Directory -Force -Path $tlsDir | Out-Null
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$jwt = [byte[]]::new(48)
$rng.GetBytes($jwt)
$jwtSecret = [Convert]::ToBase64String($jwt).TrimEnd('=').Replace('+','-').Replace('/','_')
[System.IO.File]::WriteAllText((Join-Path $secretDir 'jwt_secret'), $jwtSecret, [System.Text.Encoding]::ASCII)
$redisBytes = [byte[]]::new(48)
$rng.GetBytes($redisBytes)
$redisPassword = [Convert]::ToBase64String($redisBytes).TrimEnd('=').Replace('+','-').Replace('/','_')
[System.IO.File]::WriteAllText((Join-Path $secretDir 'redis_password'), $redisPassword, [System.Text.Encoding]::ASCII)
[System.IO.File]::WriteAllText((Join-Path $secretDir 'redis_acl'), "user default on >$redisPassword ~* &* +@all`n", [System.Text.Encoding]::ASCII)
$adapterBytes = [byte[]]::new(48)
$rng.GetBytes($adapterBytes)
$adapterToken = [Convert]::ToBase64String($adapterBytes).TrimEnd('=').Replace('+','-').Replace('/','_')
[System.IO.File]::WriteAllText((Join-Path $secretDir 'edu_adapter_token'), $adapterToken, [System.Text.Encoding]::ASCII)
$openssl = Get-Command openssl -ErrorAction SilentlyContinue
if (-not $openssl) { throw 'Install OpenSSL, or provision TLS PEM files from your certificate authority.' }
& $openssl.Source req -x509 -nodes -newkey rsa:3072 -days 30 -keyout (Join-Path $tlsDir 'privkey.pem') -out (Join-Path $tlsDir 'fullchain.pem') -subj '/CN=localhost' -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1'
if ($LASTEXITCODE -ne 0) { throw 'OpenSSL could not create the local-only self-signed certificate.' }
Write-Host 'Local-only JWT/Redis secrets and a 30-day self-signed TLS certificate created.'
Write-Host 'This certificate is for smoke runs only; production must mount CA-issued fullchain.pem and privkey.pem.'
$envFile = Join-Path $PSScriptRoot '.env'
if (-not (Test-Path -LiteralPath $envFile)) {
    [System.IO.File]::WriteAllText($envFile, "EDU_DEMO_MODE=1`n", [System.Text.Encoding]::ASCII)
}
Write-Host 'Local demo mode is enabled in edu_dss/.env; remove it and configure real adapters for production.'
