param(
    [string]$Message = "Backup sync $(Get-Date -Format 'yyyy-MM-dd HH:mm')",
    [string]$Remote = 'origin',
    [string]$ExpectedRemoteUrl = 'https://github.com/PebblesAndMarbles/sDTT_rev01.git',
    [switch]$SkipSizeAudit
)

$ErrorActionPreference = 'Stop'

$currentBranch = git branch --show-current
if (-not $currentBranch) {
    throw 'Unable to determine current branch.'
}

$actualRemoteUrl = git remote get-url $Remote
if ($actualRemoteUrl -ne $ExpectedRemoteUrl) {
    throw "Remote '$Remote' points to '$actualRemoteUrl', expected '$ExpectedRemoteUrl'."
}

Write-Host "Current branch: $currentBranch"
Write-Host "Remote: $Remote -> $actualRemoteUrl"

if (-not $SkipSizeAudit) {
    Write-Host 'Running size audit...'
    powershell -ExecutionPolicy Bypass -File .\scripts\size_audit.ps1
    if ($LASTEXITCODE -ne 0) {
        throw "Size audit failed (exit code $LASTEXITCODE). Resolve large-file issues before pushing."
    }
}

Write-Host 'Staging code/docs scope...'
$stagePaths = @(
    '.gitignore'
    'README.md'
    '*.py'
    '*.jsl'
    '*.md'
    '*.code-workspace'
    '*.txt'
    '.github/'
)

$excludePaths = @(
    ':(exclude)debug/'
    ':(exclude)Old/'
    ':(exclude)integrated_output/'
    ':(exclude)flag_images/'
    ':(exclude)logs/'
    ':(exclude)**/debug/'
    ':(exclude)**/integrated_output/'
    ':(exclude)**/flag_images/'
    ':(exclude)**/logs/'
    ':(exclude)**/__pycache__/'
    ':(exclude)BOST/bost_debug/'
    ':(exclude)**/*debug*.txt'
    ':(exclude)**/*.jrn'
    ':(exclude)**/*.xls'
    ':(exclude)integrated_output/*.csv'
    ':(exclude)BOST/*.csv'
    ':(exclude)**/debug/*.csv'
    ':(exclude)**/*.log'
    ':(exclude)**/*.jmp'
    ':(exclude)**/*.VG2'
)

git add --all -- @stagePaths @excludePaths

$staged = git diff --cached --name-only
if (-not $staged) {
    Write-Host 'No staged changes to commit.'
    exit 0
}

Write-Host 'Committing...'
git commit -m $Message

Write-Host "Pushing $currentBranch to $Remote..."
git push -u $Remote $currentBranch

Write-Host 'Backup push complete.'