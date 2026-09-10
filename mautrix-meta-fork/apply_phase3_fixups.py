#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys


def replace_exact(path: pathlib.Path, old: str, new: str, expected: int) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != expected:
        raise SystemExit(f"{path}: expected {expected} occurrences of {old!r}, found {count}")
    path.write_text(text.replace(old, new))


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_phase3_fixups.py <patched-mautrix-meta-source-dir>")
    root = pathlib.Path(sys.argv[1]).resolve()

    # wrapAvatar became account-aware in the first Phase 3 patch. All MetaClient
    # call sites must use that receiver; leaving any global call would either fail
    # to compile or reintroduce unscoped media egress.
    replace_exact(root / "pkg/connector/chatinfo.go", "wrapAvatar(", "m.wrapAvatar(", 2)
    replace_exact(root / "pkg/connector/handlemeta.go", "wrapAvatar(evt.ImageURL)", "m.wrapAvatar(evt.ImageURL)", 1)

    # http.DefaultTransport has ProxyFromEnvironment by default. The isolation
    # test needs an intentionally clean baseline so it can prove our helper does
    # not mutate the process-global transport.
    replace_exact(
        root / "pkg/msgconv/mediadl/proxy_context_test.go",
        "mediaHTTPClient = &http.Client{Transport: http.DefaultTransport.(*http.Transport).Clone()}",
        "mediaHTTPClient = &http.Client{Transport: &http.Transport{}}",
        1,
    )

    print(f"Applied Phase 3 follow-up fixes to {root}")


if __name__ == "__main__":
    main()
