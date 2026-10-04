# Generates a self-signed localhost TLS certificate for the local nginx proxy.
#
# This produces DEV-ONLY certificates (CN=localhost, valid ~365 days). It must
# never be used for production. Production TLS should terminate via a trusted
# CA (e.g. Let's Encrypt) or your organization's CA.
#
# The generated key/cert are written to nginx/certs/tls.key and tls.crt, which
# the nginx container mounts read-only at /etc/nginx/certs. Both files are
# git-ignored and must never be committed to version control.
#
# Requires: openssl on PATH. Run from the repository root:
#   powershell -ExecutionPolicy Bypass -File infrastructure/nginx/gen_self_signed_certs.ps1

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$certsDir = Join-Path $repoRoot "nginx\certs"
$keyPath = Join-Path $certsDir "tls.key"
$crtPath = Join-Path $certsDir "tls.crt"

if (-not (Test-Path -LiteralPath $certsDir)) {
    New-Item -ItemType Directory -Path $certsDir -Force | Out-Null
    "created certs directory: $certsDir"
}

& openssl req -x509 -newkey rsa:2048 -nodes -days 365 `
    -keyout $keyPath `
    -out $crtPath `
    -subj "/CN=localhost" `
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"

if ($LASTEXITCODE -ne 0) {
    throw "openssl failed; is openssl installed and on PATH?"
}

# The key is private material; restrict its ACL to the current user.
icacls $keyPath /inheritance:r /grant:r "$env:USERNAME:(R,W)"

""
"Generated DEV-ONLY self-signed certificate:"
"  cert : $crtPath"
"  key  : $keyPath"
""
"This key/cert are git-ignored. Do not commit them. Restart nginx to pick up changes."
