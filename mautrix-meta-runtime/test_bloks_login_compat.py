#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import pathlib
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "apply_bloks_login_compat", HERE / "apply_bloks_login_compat.py"
)
mod = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(mod)


class BloksLoginCompatPatchTests(unittest.TestCase):
    def source(self):
        return mod.OLD + "\n" + mod.ASSERT_TYPE_OLD + "\n"

    def test_backports_upstream_i64_convert(self):
        patched = mod.patch_interp(self.source())
        self.assertIn('case "bk.action.i64.Convert":', patched)
        self.assertIn('case int64:', patched)
        self.assertIn('case float64:', patched)
        self.assertIn('BloksLiteralOf(int64(val))', patched)
        self.assertIn('can\'t convert %T to i64', patched)

    def test_backports_upstream_numeric_union_assertion(self):
        patched = mod.patch_interp(self.source())
        self.assertIn("if expected == 100 {", patched)
        self.assertIn("case 3, 4:", patched)
        self.assertIn("actual = expected", patched)
        self.assertNotIn(mod.ASSERT_TYPE_OLD, patched)

    def test_patch_is_fail_closed_if_convert_anchor_changes(self):
        with self.assertRaises(RuntimeError):
            mod.patch_interp(mod.ASSERT_TYPE_OLD)

    def test_patch_is_fail_closed_if_assert_anchor_changes(self):
        with self.assertRaises(RuntimeError):
            mod.patch_interp(mod.OLD)

    def test_patch_refuses_double_application(self):
        once = mod.patch_interp(self.source())
        with self.assertRaises(RuntimeError):
            mod.patch_interp(once)


if __name__ == "__main__":
    unittest.main()
