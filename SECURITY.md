# Security policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository. Please do
not publish exploit details, leaked credentials, or user data in an issue.

Include the affected version, reproduction steps, expected impact, and any
suggested mitigation. Reports are reviewed on a best-effort basis.

## Secrets

The repository must never contain real credentials or runtime data. Keep all
real values in an untracked `.env` file or in your deployment platform's
secret store.

If a credential is accidentally committed:

1. Revoke or rotate it immediately at its provider.
2. Remove it from Git history before making the repository public.
3. Run `python scripts/security_audit.py` and inspect the complete history.
4. Treat any exposed database encryption key as a data incident.

Deleting a secret only from the latest commit is not sufficient.

## Data model limitations

Selected SQLite fields are encrypted with Fernet, but dates and scheduling
metadata remain queryable so reminders can be scheduled efficiently. The
running process can decrypt data and the optional AI integration sends the
context described by `/privacy` to an external provider. This is application-
level encryption, not end-to-end or zero-knowledge encryption.

