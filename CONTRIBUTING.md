# Contributing

Thank you for helping improve SpendAlert.

## Local setup

Follow the Python quick start in `README.md`, then run:

```bash
python -m pip install --requirement requirements-dev.txt
python scripts/security_audit.py
ruff check .
python test_bot.py
```

All of them must pass before a pull request is opened. Lint exceptions live in
`pyproject.toml` and each one is justified there — add a rule to that list only
with a reason, never to silence a finding.

Automated coding agents must also follow the public repository contract in
`AGENTS.md`.

## Pull requests

- Keep changes focused and explain user-visible behavior.
- Add or update a test for behavior changes.
- Never commit `.env`, tokens, API keys, chat IDs, databases, logs, backups,
  server credentials, or machine-specific paths.
- Do not weaken encryption or privacy notices without documenting the impact.
- Preserve compatibility with Python 3.10 and 3.12.

## Security issues

Do not open a public issue for a vulnerability or suspected secret leak.
Follow `SECURITY.md` instead.
