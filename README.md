# sDTT Rev01 - GitHub Backup Repository

This repository is a code and documentation backup for the sDTT Rev01 workspace.

## Scope for initial backup

Tracked content:
- Python source (`.py`)
- JMP scripts (`.jsl`)
- Documentation (`.md`)
- Workspace config (`.code-workspace`)
- Small supporting text files (`.txt`) when needed

Excluded content:
- Generated CSV outputs in `integrated_output/`
- Intermediate query chunks in `integrated_output/query_files/`
- Generated plots in `flag_images/`
- Runtime logs in `logs/`
- Debug CSV dumps under `debug/`
- Legacy archive folder `Old/` (excluded for first push)

## Why this split

This network workspace contains many generated CSV and log files, including files larger than 50 MB.
Keeping generated artifacts out of Git makes the backup reliable, faster to clone, and easier to review.

## Pre-push safety checks

Run the size audit script before pushing:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\size_audit.ps1
```

Recommended checks before each push:
1. `git status --short`
2. `git diff --cached --name-only`
3. Confirm no generated CSV/image/log paths are staged
4. Confirm no staged file exceeds 10 MB (hard stop if any file exceeds 50 MB)

## Initial Git setup on this network path

If Git blocks operations due to UNC ownership checks, add this directory once:

```powershell
git config --global --add safe.directory "//orshfs.intel.com/ORAnalysis$/1276_MAODATA/Config/etch/AME/tbatson/sDTT/sDTT_rev01"
```

Then initialize/push as normal:

```powershell
git init
git branch -M main
git add .
git commit -m "Initial code/docs backup baseline"
git remote add origin <your-private-github-repo-url>
git push -u origin main
```
