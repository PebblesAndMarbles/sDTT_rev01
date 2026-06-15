# GitHub Push Workflow for sDTT Rev01

This document describes the current push workflow for the sDTT Rev01 backup repository, including the account context, the kinds of files that are expected to be pushed, and the checks we use before publishing changes to GitHub.

## Repository target

- GitHub repository: `PebblesAndMarbles/sDTT_rev01`
- Local branch used for publishing: `main`
- Remote name: `origin`

## Account context

The repository can be used in environments where the Git author identity and the GitHub sign-in identity are not the same thing.

- Git author name: `Batson III, William`
- Git author email: `william.batson.iii@intel.com`
- GitHub remote owner for this repo: `PebblesAndMarbles`

If push behavior changes because of enterprise policy or credential changes, verify the active GitHub sign-in in Windows Credential Manager or Git Credential Manager before retrying the push.

## What we are pushing

This repository is a code and documentation backup for the sDTT Rev01 workspace. The intent is to track source and human-readable support files that help reproduce or explain the workflow.

Typical files that should be pushed:

- Python source files such as `.py`
- JMP scripts such as `.jsl`
- Documentation such as `.md`
- Workspace configuration such as `.code-workspace`
- Small supporting text files such as `.txt` when they are part of the workflow

Typical files that should stay out of Git:

- Generated CSV outputs in `integrated_output/`
- Intermediate query chunks in `integrated_output/query_files/`
- Generated plots in `flag_images/`
- Runtime logs in `logs/`
- Debug CSV dumps and investigation artifacts under `debug/`
- Local archive material under `Old/`
- Large or temporary files that are not needed to reconstruct the code or documentation flow

Practical default: for this repo, `debug/` and `Old/` should normally be treated as **non-push folders**.
Only make an exception when a specific debug artifact or archived note is truly needed to preserve reproducible technical context.

### Cleanup and archive policy

When one-time investigation assets are no longer part of the active workflow:

- Keep reusable source code and maintained docs in normal repo locations.
- Move one-time utilities, temporary probes, and obsolete debug packages into `Old/` if they must be retained locally.
- Do not include archived cleanup folders or bulk debug trees in routine GitHub backup pushes.

Example from June 2026 APC cleanup:

- `debug/mwe_apc/` was archived locally instead of kept in the active debug tree.
- One-time APC probe/verification scripts were moved out of `scripts/` into an `Old/` archive folder.
- The maintained diagnostic utility `scripts/apc_mwe_diagnostics.py` remained active.

## Recommended push flow

1. Check the current branch and remote.
2. Review the working tree with `git status -sb`.
3. Confirm the files staged for commit with `git diff --cached --name-only`.
4. Verify that no generated output, log, or large artifact files are staged.
5. Commit the intended changes.
6. Push the current branch with `git push -u origin main` when the branch is first connected, or `git push` for normal updates.

## Practical checks before pushing

- Run `git remote -v` and confirm the remote points to the expected repo.
- Run `git status --short` and make sure only intended files are present.
- Check specifically that `debug/` and `Old/` are not staged unless you intentionally need one of those files in Git.
- If a push fails with a repository-not-found or auth error, confirm that the repo exists and that the signed-in GitHub account has access.
- If needed, clear and re-add the `github.com` credential entry in Windows Credential Manager and retry the push.

## Automation helper

The repo includes a helper script at [scripts/backup_push.ps1](scripts/backup_push.ps1) that automates the usual backup flow:

1. Verifies the current branch and remote target.
2. Runs the size audit unless you explicitly skip it.
3. Stages the source and documentation scope while leaving generated/debug/archive content out.
4. Commits with a timestamped message, unless you provide one.
5. Pushes the current branch back to the expected GitHub repo.

Typical usage:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\backup_push.ps1
```

Optional custom commit message:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\backup_push.ps1 -Message "Update push workflow docs"
```

If you need to bypass the size audit for a local experiment, use `-SkipSizeAudit` only after checking the staged set manually.

## Relationship to the rest of the repo

This workflow supports the broader sDTT code and documentation tree, including the pipeline scripts, flagging engines, JSL utilities, and supporting knowledge-base notes. The goal is to keep the GitHub repo focused on the reproducible source and reference material rather than generated outputs.