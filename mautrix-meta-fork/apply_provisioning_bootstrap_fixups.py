#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_provisioning_bootstrap_fixups.py <patched-mautrix-meta-source-dir>")
    root = pathlib.Path(sys.argv[1]).resolve()
    login = root / "pkg/connector/login.go"
    text = login.read_text()
    lines = text.splitlines(keepends=True)
    changed = 0
    for idx, line in enumerate(lines):
        if "CookiesParams.WaitForURLPattern" not in line:
            continue
        fixed = line.replace(r"\.", r"\\.").replace(r"\?", r"\\?")
        if fixed != line:
            lines[idx] = fixed
            changed += 1
    if changed != 3:
        raise SystemExit(f"{login}: expected to repair 3 cookie URL patterns, repaired {changed}")
    login.write_text("".join(lines))
    print(f"Applied provisioning bootstrap escape fixups to {root}")


if __name__ == "__main__":
    main()
