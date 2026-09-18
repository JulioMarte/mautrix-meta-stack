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

ASSERT_TYPE_OLD = '''\t\tactual, err := getBloksType(val)
\t\tif err != nil {
\t\t\treturn nil, err
\t\t}
\t\tif expected != actual {
\t\t\treturn nil, fmt.Errorf("bloks type assertion failure (%d != %d)", actual, expected)
\t\t}
'''

ASSERT_TYPE_NEW = '''\t\tactual, err := getBloksType(val)
\t\tif err != nil {
\t\t\treturn nil, err
\t\t}
\t\t// Native Bloks uses 100 as the numeric union type: either int or float.
\t\t// Newer Facebook 2FA payloads rely on this assertion.
\t\tif expected == 100 {
\t\t\tswitch actual {
\t\t\tcase 3, 4:
\t\t\t\tactual = expected
\t\t\t}
\t\t}
\t\tif expected != actual {
\t\t\treturn nil, fmt.Errorf("bloks type assertion failure (%d != %d)", actual, expected)
\t\t}
'''


def patch_interp(text: str) -> str:
    convert_count = text.count(OLD)
    if convert_count != 1:
        raise RuntimeError(
            "pkg/messagix/bloks/interp.go: expected exactly one i64.Convert "
            f"upstream anchor, found {convert_count}"
        )
    if 'case "bk.action.i64.Convert":' in text:
        raise RuntimeError("pkg/messagix/bloks/interp.go: i64.Convert is already implemented")

    assert_count = text.count(ASSERT_TYPE_OLD)
    if assert_count != 1:
        raise RuntimeError(
            "pkg/messagix/bloks/interp.go: expected exactly one AssertType "
            f"upstream anchor, found {assert_count}"
        )
    if "if expected == 100 {" in text:
        raise RuntimeError(
            "pkg/messagix/bloks/interp.go: numeric AssertType compatibility is already implemented"
        )

    return text.replace(OLD, NEW, 1).replace(ASSERT_TYPE_OLD, ASSERT_TYPE_NEW, 1)


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
