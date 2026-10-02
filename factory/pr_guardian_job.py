"""Script-only Guardian job: report merges/errors and stay silent when idle."""
import json
from pathlib import Path
import subprocess
import sys


def main():
    result = subprocess.run(
        [sys.executable, str(Path.home() / "scripts/pr-guardian/sweep.py"), *sys.argv[1:]],
        capture_output=True, text=True,
    )
    try:
        report = json.loads(result.stdout.rsplit("---JSON---", 1)[1])
        if report["merged"] or report["errors"]:
            print(json.dumps({"merged": report["merged"], "errors": report["errors"]}))
    except (IndexError, KeyError, TypeError, ValueError):
        print("PR Guardian failed to produce its structured report.")
        return 1
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
