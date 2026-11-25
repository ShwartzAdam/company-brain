#!/usr/bin/env bash
# Fails if a staged file looks like a secret, or if .env / data/ / *.db is staged.
set -u
bad=0
if git diff --cached --name-only | grep -E '(^|/)\.env$|^data/|\.db$|\.sqlite$' ; then
  echo "ERROR: forbidden file staged"; bad=1
fi
if git diff --cached -U0 -- . ':!scripts/secret_scan.sh' | grep -E '^\+' | grep -E 'AKIA[0-9A-Z]{16}|github_pat_|ghp_[A-Za-z0-9]{20,}|sk-lf-|pk-lf-|aws_secret_access_key *= *[A-Za-z0-9/+]{20,}' ; then
  echo "ERROR: secret-like text staged"; bad=1
fi
[ $bad -eq 0 ] && echo "secret scan clean"
exit $bad
