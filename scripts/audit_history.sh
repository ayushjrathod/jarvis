#!/usr/bin/env bash
# Audit git history (and the worktree) for things a public repo must not hold.
# Read-only: lists matches, never rewrites. Run before pushing, and after any
# incident — it answers "what leaked, and in which commits" in one pass.
#
# Covers the session-30/31 findings: vault personal subtrees (now ignored but
# published in history), live queue drops, client secrets, API keys, and any
# non-markdown ever committed under vault/ or data/ (the inbox invites PDFs).
set -uo pipefail
cd "$(dirname "$0")/.."

fail=0
say()  { printf '%s\n' "$1"; }
bad()  { printf 'LEAK: %s\n' "$1"; fail=1; }
ok()   { printf 'ok: %s\n' "$1"; }

# 1. Live secrets must be untracked AND ignored (existing on disk is fine —
# spotify.json holds the working credentials; committed is the leak).
for f in .env data/spotify.json; do
  if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
    bad "$f is TRACKED"
  else
    ok "$f untracked"
  fi
  if git check-ignore -q "$f" 2>/dev/null; then
    ok "$f ignored"
  else
    bad "$f not ignored"
  fi
done

# 2. API-key-shaped strings anywhere in history (sk-ant-, ghp_, xoxb-).
if git log --all -p -S 'sk-ant-' -- . ':!*.lock' 2>/dev/null | grep -q '^commit'; then
  bad "sk-ant- string in history:"; git log --all --oneline -S 'sk-ant-' | head -n 5
else
  ok "no sk-ant- in history"
fi
for pat in 'ghp_' 'xoxb-'; do
  if git log --all -p -S "$pat" 2>/dev/null | grep -q '^commit'; then
    bad "$pat string in history"
  else
    ok "no $pat in history"
  fi
done

# 3. Secrets ever committed (even if later removed).
for f in .env data/spotify.json; do
  if git log --all --oneline -- "$f" | grep -q .; then
    bad "$f appears in history:"; git log --all --oneline -- "$f" | head -n 5
  else
    ok "$f never committed"
  fi
done

# 4. Non-markdown ever committed under vault/ or data/ (bank-statement shape).
leaks=$(git log --all --diff-filter=A --name-only --format='' -- vault/ data/ \
  | grep -vE '\.(md|json|yaml|yml)$' | grep -v '^$' | sort -u || true)
if [ -n "$leaks" ]; then
  bad "non-doc files committed under vault//data:"; printf '%s\n' "$leaks" | head -n 10
else
  ok "only docs under vault//data in history"
fi

# 5. Personal subtrees currently tracked (should be READMEs/notes only).
tracked=$(git ls-files vault/briefs vault/memory vault/tasks 'vault/inbox/*' \
  ':!vault/inbox/README.md' 2>/dev/null || true)
if [ -n "$tracked" ]; then
  bad "personal vault files still tracked:"; printf '%s\n' "$tracked" | head -n 10
else
  ok "no personal vault files tracked"
fi

if [ "$fail" -eq 0 ]; then say "history audit clean"; else say "history audit FOUND LEAKS (see above)"; fi
exit "$fail"
