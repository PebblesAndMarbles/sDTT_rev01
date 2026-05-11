$ErrorActionPreference = 'Stop'

$root = Get-Location
$files = Get-ChildItem -Path $root -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notmatch '\\.git\\' }

if (-not $files) {
    Write-Host "No files found under $root"
    exit 0
}

Write-Host "Root: $root"
Write-Host ""

Write-Host "== Top 25 largest files =="
$files |
    Sort-Object Length -Descending |
    Select-Object -First 25 FullName,
        @{Name='SizeMB';Expression={[math]::Round($_.Length / 1MB, 2)}} |
    Format-Table -AutoSize

Write-Host ""
Write-Host "== Extension summary (top 20 by total size) =="
$files |
    Group-Object Extension |
    ForEach-Object {
        $sum = ($_.Group | Measure-Object Length -Sum).Sum
        $max = ($_.Group | Sort-Object Length -Descending | Select-Object -First 1).Length
        [pscustomobject]@{
            Extension = if ([string]::IsNullOrWhiteSpace($_.Name)) { '<none>' } else { $_.Name }
            Count     = $_.Count
            TotalMB   = [math]::Round($sum / 1MB, 2)
            MaxMB     = [math]::Round($max / 1MB, 2)
        }
    } |
    Sort-Object TotalMB -Descending |
    Select-Object -First 20 |
    Format-Table -AutoSize

Write-Host ""
Write-Host "== Files over 10 MB =="
$over10 = $files |
    Where-Object { $_.Length -gt 10MB } |
    Sort-Object Length -Descending |
    Select-Object FullName,
        @{Name='SizeMB';Expression={[math]::Round($_.Length / 1MB, 2)}}

if ($over10) {
    $over10 | Format-Table -AutoSize
} else {
    Write-Host "None"
}

Write-Host ""
Write-Host "== Files over 50 MB (hard stop for normal Git tracking) =="
$over50 = $files |
    Where-Object { $_.Length -gt 50MB } |
    Sort-Object Length -Descending |
    Select-Object FullName,
        @{Name='SizeMB';Expression={[math]::Round($_.Length / 1MB, 2)}}

if ($over50) {
    $over50 | Format-Table -AutoSize
    Write-Host ""
    Write-Warning "One or more files exceed 50 MB. Keep these out of regular Git commits."
    exit 2
} else {
    Write-Host "None"
}

exit 0
