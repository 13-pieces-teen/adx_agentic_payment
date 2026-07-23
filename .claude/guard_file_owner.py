#!/usr/bin/env python3
"""File ownership guard for Claude Code PreToolUse hook."""
import subprocess, sys, os

YOUR_EMAILS = ["235239800+sunruize93-cmyk@users.noreply.github.com", "sunruize93@gmail.com"]
YOUR_NAMES = ["sunruize", "Srzzz"]
YOUR_MODULES = ["matching/", ".claude/", ".githooks/", "web/"]

def run(cmd):
    try: return subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL, text=True).strip()
    except: return ""

def is_my_file(filepath):
    repo = run("git rev-parse --show-toplevel")
    if not repo: return True, "not in git repo"
    rel = os.path.relpath(filepath, repo)
    for mod in YOUR_MODULES:
        if rel.startswith(mod) or f"/{mod}" in rel:
            return True, f"your module '{mod}'"
    if not os.path.exists(filepath): return True, "new file"
    # New file not in HEAD → always allowed
    if run(f"git cat-file -e 'HEAD:{rel}' 2>/dev/null") == "" and run(f"git cat-file -e 'HEAD:{rel}' 2>&1").find("fatal") >= 0:
        pass  # fall through to check
    else:
        pass  # file exists in HEAD, check author
    # Actually, simpler: if file not in git at all, allow it
    if not run(f"git ls-files '{filepath}' 2>/dev/null"): return True, "untracked/new file"
    # Check if file exists in HEAD
    in_head = run(f"git cat-file -e 'HEAD:{rel}' 2>/dev/null; echo $?")
    if in_head.strip() != "0": return True, "new file (not in HEAD)"
    author = run(f"git log -1 --format='%an' -- '{filepath}'")
    email = run(f"git log -1 --format='%ae' -- '{filepath}'")
    if not author: return True, "no commit history"
    if author in YOUR_NAMES or email in YOUR_EMAILS: return True, f"yours ({author})"
    return False, f"owned by {author} <{email}> — NOT YOU"

if __name__ == "__main__":
    filepath = sys.argv[1] if len(sys.argv) > 1 else ""
    if not filepath:
        sys.exit(0)
    ok, reason = is_my_file(filepath)
    if ok:
        sys.exit(0)
    print(f"⛔ GUARD: {filepath} — {reason}", file=sys.stderr)
    sys.exit(1)
