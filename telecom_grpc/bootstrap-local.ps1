$ErrorActionPreference = 'Stop'
$secretDir = Join-Path $PSScriptRoot 'secrets'
$tlsDir = Join-Path $PSScriptRoot 'tls'
New-Item -ItemType Directory -Force -Path $secretDir, $tlsDir | Out-Null
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$bytes = [byte[]]::new(48)
$rng.GetBytes($bytes)
$password = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
[System.IO.File]::WriteAllText((Join-Path $secretDir 'redis_password'), $password, [System.Text.Encoding]::ASCII)
[System.IO.File]::WriteAllText((Join-Path $secretDir 'redis_acl'), "user default on >$password ~* &* +@all`n", [System.Text.Encoding]::ASCII)
$openssl = Get-Command openssl -ErrorAction SilentlyContinue
if (-not $openssl) { throw 'Install OpenSSL or provision mTLS certificates from your PKI.' }
& $openssl.Source req -x509 -nodes -newkey rsa:3072 -days 30 -keyout (Join-Path $tlsDir 'ca.key') -out (Join-Path $tlsDir 'client-ca.crt') -subj '/CN=Telecom Local Test CA' -addext 'basicConstraints=critical,CA:TRUE'
if ($LASTEXITCODE -ne 0) { throw 'Could not create local CA.' }
$serverExt = Join-Path $tlsDir 'server.ext'
[System.IO.File]::WriteAllText($serverExt, "subjectAltName=DNS:localhost,DNS:call-policy-grpc`nextendedKeyUsage=serverAuth`nkeyUsage=digitalSignature,keyEncipherment`n", [System.Text.Encoding]::ASCII)
& $openssl.Source req -new -nodes -newkey rsa:3072 -keyout (Join-Path $tlsDir 'server.key') -out (Join-Path $tlsDir 'server.csr') -subj '/CN=localhost'
if ($LASTEXITCODE -ne 0) { throw 'Could not create local server key.' }
& $openssl.Source x509 -req -in (Join-Path $tlsDir 'server.csr') -CA (Join-Path $tlsDir 'client-ca.crt') -CAkey (Join-Path $tlsDir 'ca.key') -CAcreateserial -out (Join-Path $tlsDir 'server.crt') -days 30 -extfile $serverExt
if ($LASTEXITCODE -ne 0) { throw 'Could not sign local server certificate.' }
$clientExt = Join-Path $tlsDir 'client.ext'
[System.IO.File]::WriteAllText($clientExt, "extendedKeyUsage=clientAuth`nkeyUsage=digitalSignature,keyEncipherment`n", [System.Text.Encoding]::ASCII)
& $openssl.Source req -new -nodes -newkey rsa:3072 -keyout (Join-Path $tlsDir 'client.key') -out (Join-Path $tlsDir 'client.csr') -subj '/CN=Telecom Local Test Client'
if ($LASTEXITCODE -ne 0) { throw 'Could not create local client key.' }
& $openssl.Source x509 -req -in (Join-Path $tlsDir 'client.csr') -CA (Join-Path $tlsDir 'client-ca.crt') -CAkey (Join-Path $tlsDir 'ca.key') -CAcreateserial -out (Join-Path $tlsDir 'client.crt') -days 30 -extfile $clientExt
if ($LASTEXITCODE -ne 0) { throw 'Could not sign local client certificate.' }
Remove-Item -LiteralPath (Join-Path $tlsDir 'ca.key')
Write-Host 'Local-only Redis credentials and mTLS test identities created. Never use them in production.'
