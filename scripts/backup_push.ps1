$ErrorActionPreference = 'Stop'

param(
    [string]$Message = "Backup sync $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
)

Write-Host "Running size audit..."
powershell -ExecutionPolicy Bypass -File .\scripts\size_audit.ps1
if ($LASTEXITCODE -ne 0) {
    throw "Size audit failed (exit code $LASTEXITCODE). Resolve large-file issues before pushing."
}

Write-Host "Staging code/docs scope..."
git add -- '.gitignore' 'README.md' 'scripts/*.ps1' '*.py' '*.jsl' '*.md' '*.code-workspace' '*.txt'

$staged = git diff --cached --name-only
if (-not $staged) {
    Write-Host "No staged changes to commit."
    exit 0
}

Write-Host "Committing..."
git commit -m $Message

Write-Host "Pushing to origin/main..."
git push

Write-Host "Backup push complete."