# Credential-history cleanup proposal

This is a proposal only. No backup files were deleted, no Git objects were
rewritten, and no history was force-pushed.

## Legacy source backups

Review and approve these files before removal or secure archival:

- `app/app.py.before-pdf-fix`
- `app/app.py.before-gateway-fix`
- `app/app.py.pre-final-patch`
- `app/app.py.before-security-fixes`
- `app/app_recovered.py`

For each file, first confirm it is not required for legal, incident, or
rollback evidence. Preserve a restricted, access-logged forensic copy if
required. Otherwise remove it in a separate reviewed change after all exposed
credentials have been rotated.

## Git objects and distribution copies

The repository has no reachable commit history in this workspace, but
`git fsck` reported dangling objects containing historical source. Treat all
copies as exposed. Search mirrors, build caches, container registries,
archives, and deployment workspaces before declaring remediation complete.

If history cleanup is approved:

1. Create and verify a full repository backup.
2. Use an approved history-rewrite tool to remove credential-bearing paths or
   content.
3. Force-push only after maintainer approval and coordinated access review.
4. Invalidate old clones, CI caches, artifacts, and image layers.
5. Verify the rewritten repository and record the old/new references.

Credential rotation remains mandatory even if history is rewritten.
