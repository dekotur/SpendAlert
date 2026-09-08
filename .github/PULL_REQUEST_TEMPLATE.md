## Why

What user or engineering problem does this solve?

## What changed

Describe the behavior change and the important implementation decisions.

## Evidence

Include tests, reproducible steps, or screenshots made only with synthetic data.

## Safety checklist

- [ ] I ran `python scripts/security_audit.py`.
- [ ] I ran `ruff check .` and it passed.
- [ ] I ran `python test_bot.py` and all 56 groups passed.
- [ ] I ran `python -m compileall -q .` and `git diff --check`.
- [ ] I inspected `git status --short` before committing.
- [ ] I added no real token, chat ID, key, database, log, backup, `.ics`, local path, hostname, IP, or user data.
- [ ] I updated README, `.env.example`, privacy text, or `AGENTS.md` when the public contract changed.
