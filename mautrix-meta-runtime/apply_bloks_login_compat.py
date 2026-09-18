#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import subprocess
import sys

UPSTREAM_SHA = "001f276beca5b90dead1bbc1351e1036e3f966a7"

OLD = '''\tcase "bk.action.i64.Const":
\t\treturn i.Evaluate(ctx, &call.Args[0])
\tcase "bk.action.map.Get":
'''

NEW = '''\tcase "bk.action.i64.Const":
\t\treturn i.Evaluate(ctx, &call.Args[0])
\tcase "bk.action.i64.Convert":
\t\targ, err := i.Evaluate(ctx, &call.Args[0])
\t\tif err != nil {
\t\t\treturn nil, err
\t\t}
\t\tswitch val := arg.Value().(type) {
\t\tcase int64:
\t\t\treturn BloksLiteralOf(val), nil
\t\tcase float64:
\t\t\treturn BloksLiteralOf(int64(val)), nil
\t\t}
\t\treturn nil, fmt.Errorf("can't convert %T to i64", arg.Value())
\tcase "bk.action.map.Get":
'''


def patch_interp(text: str) -> str:
    count = text.count(OLD)
    if count != 1:
        raise RuntimeError(
            f"pkg/messagix/bloks/interp.go: expected exactly one upstream anchor, found {count}"
        )
    if 'case "bk.action.i64.Convert":' in text:
        raise RuntimeError("pkg/messagix/bloks/interp.go: i64.Convert is already implemented")
    return text.replace(OLD, NEW, 1)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_bloks_login_compat.py <mautrix-meta-source-dir>")
    root = pathlib.Path(sys.argv[1]).resolve()
    actual_sha = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual_sha != UPSTREAM_SHA:
        raise SystemExit(
            f"unexpected mautrix-meta upstream SHA: {actual_sha}; expected {UPSTREAM_SHA}"
        )

    interp = root / "pkg/messagix/bloks/interp.go"
    try:
        interp.write_text(patch_interp(interp.read_text()))
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
