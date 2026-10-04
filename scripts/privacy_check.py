"""Fail if any repository file contains private data (e.g. the home address).

  python scripts/privacy_check.py --patterns "regex1|regex2"
  PRIVATE_PATTERNS="regex1|regex2" python scripts/privacy_check.py

The patterns are never stored in the (public) repo: CI passes them from a secret, and the
research agent gets them in its private prompt. Matching is case-insensitive.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def repo_files():
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [ROOT / p for p in out.splitlines() if p]


def find(paths, patterns, root):
    """'file:line' for every line matching the patterns (case-insensitive)."""
    rx = re.compile(patterns, re.IGNORECASE)
    hits = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{path.relative_to(root).as_posix()}:{n}")
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--patterns", default=os.environ.get("PRIVATE_PATTERNS", ""))
    args = ap.parse_args()
    if not args.patterns.strip():
        print("privacy_check: no patterns given (set PRIVATE_PATTERNS)", file=sys.stderr)
        return 2
    hits = find(repo_files(), args.patterns, ROOT)
    if hits:
        # Print locations only, never the matched text: CI logs of a public repo are public too.
        print("PRIVATE DATA FOUND in:\n  " + "\n  ".join(hits), file=sys.stderr)
        return 1
    print("privacy_check: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
