"""Guard run before every automated commit: protected files may only grow.

For each staged change under the protected paths, the version at HEAD must be an exact prefix of the
staged version. Deletions, renames and in-place edits fail the check and the commit is not made.
manifest.json and data/scan/latest.json are current-state files (rewritten each run) and are deliberately
not protected; every scan is also kept as its own dated file under data/scan/.
"""
from __future__ import annotations

import subprocess
import sys

PROTECTED = ("data/ohlcv/", "data/universe/", "data/liquidity/", "data/scan/", "data/replays/", "data/outbox/",
             "data/health/runs.csv", "data/health/site-checks.csv",
             "data/health/gate-decisions.csv", "journal/", "reports/")
UNPROTECTED = ("data/ohlcv/binance/manifest.json", "data/scan/latest.json")


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def main() -> int:
    changes = git("diff", "--cached", "--name-status", "--no-renames", "HEAD").decode().splitlines()
    bad = []
    for line in changes:
        status, path = line.split("\t", 1)
        if not path.startswith(PROTECTED) or path in UNPROTECTED or status == "A":
            continue
        if status == "D":
            bad.append(f"deleted: {path}")
            continue
        old, new = git("show", f"HEAD:{path}"), git("show", f":{path}")
        if not new.startswith(old):
            bad.append(f"modified in place (not append-only): {path}")
    for b in bad:
        print(f"::error::{b}")
    print(f"append-only check: {len(changes)} staged changes, {len(bad)} violations")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
