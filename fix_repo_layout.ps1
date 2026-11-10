# Moves the flat files into app\ and app\static\ so the Dockerfile works.
# Run from PowerShell:  powershell -ExecutionPolicy Bypass -File D:\github\podcast-app\fix_repo_layout.ps1
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Repo = "D:\github\podcast-app"

function Invoke-Git {
    git @args
    if ($LASTEXITCODE -ne 0) { throw "git $($args -join ' ') failed with exit code $LASTEXITCODE" }
}

Set-Location $Repo

Write-Host "[1/5] Creating app\ and app\static\"
New-Item -ItemType Directory -Force -Path (Join-Path $Repo "app\static") | Out-Null

Write-Host "[2/5] Moving Python files into app\"
foreach ($f in @("main.py", "feeds.py", "adscan.py", "db.py")) {
    Invoke-Git mv (Join-Path $Repo $f) (Join-Path $Repo "app\$f")
}

Write-Host "[3/5] Moving index.html into app\static\"
Invoke-Git mv (Join-Path $Repo "index.html") (Join-Path $Repo "app\static\index.html")

Write-Host "[4/5] Removing the committed tarball and adding .gitignore"
Invoke-Git rm -q (Join-Path $Repo "podcast-app.tar.gz")
Set-Content -Path (Join-Path $Repo ".gitignore") -Value @("__pycache__/", "*.pyc", "*.tar.gz") -Encoding ascii
Invoke-Git add (Join-Path $Repo ".gitignore")

Write-Host "[5/5] Done. Review with: git status"
Write-Host "Then: git commit -m 'Fix layout for Docker build'; git push"
