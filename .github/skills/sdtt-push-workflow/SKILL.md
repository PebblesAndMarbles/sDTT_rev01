---
name: sdtt-push-workflow
description: "Use when preparing, reviewing, or publishing sDTT Rev01 changes to GitHub, including remote checks, staged-file review, account mismatch troubleshooting, or invoking the backup push helper script."
user-invocable: true
---

# sDTT Push Workflow Skill

Use this skill when the task is to publish sDTT Rev01 changes to GitHub or to decide whether the current working tree is safe to push.

## Source of truth

- Primary workflow reference: [markdown/GitHub_Push_Workflow.md](../../markdown/GitHub_Push_Workflow.md)
- Helper script: [scripts/backup_push.ps1](../../scripts/backup_push.ps1)
- Repo overview: [README.md](../../README.md)

## What the skill should do

1. Check the current branch, remote URL, and Git status.
2. Confirm that the staged set is limited to code, scripts, documentation, or other approved support files.
3. Warn if the user appears to be signed into the wrong GitHub account or if the remote target is not `PebblesAndMarbles/sDTT_rev01`.
4. Use the helper script for the standard push flow when the repo is clean enough to publish.
5. Stop and ask for confirmation when generated outputs, logs, or other excluded artifacts are staged.

## Guardrails

- Do not push files under `integrated_output/`, `flag_images/`, `logs/`, or debug dump paths unless the user explicitly overrides the workflow.
- Do not change the remote target unless the user has confirmed a repo move.
- If authentication fails, direct the user to Git Credential Manager or Windows Credential Manager rather than retrying blindly.

## Recommended behavior

When the working tree is ready, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\backup_push.ps1
```

If the user wants a specific commit message, pass `-Message` to the helper script. If the user asks for a policy check only, summarize the branch, remote, staged scope, and any blocked files without pushing.