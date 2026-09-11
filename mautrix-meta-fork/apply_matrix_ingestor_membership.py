#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys


def replace_once(path: pathlib.Path, old: str, new: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one occurrence, found {count}: {old!r}")
    path.write_text(text.replace(old, new, 1))


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_matrix_ingestor_membership.py <patched-mautrix-meta-source-dir>")
    root = pathlib.Path(sys.argv[1]).resolve()
    config = root / "pkg/connector/config.go"
    connector = root / "pkg/connector/connector.go"
    example = root / "pkg/connector/example-config.yaml"

    replace_once(
        config,
        '"go.mau.fi/mautrix-meta/pkg/messagix/types"\n',
        '"go.mau.fi/mautrix-meta/pkg/messagix/types"\n\t"maunium.net/go/mautrix/id"\n',
    )
    replace_once(
        config,
        '\tProxyOther         bool   `yaml:"proxy_other"`\n',
        '\tProxyOther         bool   `yaml:"proxy_other"`\n\n\tMatrixIngestorMXID id.UserID `yaml:"matrix_ingestor_mxid"`\n',
    )
    replace_once(
        config,
        '\thelper.Copy(up.Bool, "proxy_other")\n',
        '\thelper.Copy(up.Bool, "proxy_other")\n\thelper.Copy(up.Str|up.Null, "matrix_ingestor_mxid")\n',
    )
    replace_once(
        config,
        '''\t}\n\treturn nil\n}\n\ntype DisplaynameParams struct {\n''',
        '''\t}\n\tif m.Config.MatrixIngestorMXID != "" {\n\t\tif _, _, err := m.Config.MatrixIngestorMXID.Parse(); err != nil || len(m.Config.MatrixIngestorMXID) > id.UserIDMaxLength {\n\t\t\treturn fmt.Errorf("invalid matrix_ingestor_mxid %q", m.Config.MatrixIngestorMXID)\n\t\t}\n\t}\n\treturn nil\n}\n\ntype DisplaynameParams struct {\n''',
    )

    replace_once(
        example,
        '''# Should other traffic, not configured here, be proxied?\nproxy_other: true\n''',
        '''# Should other traffic, not configured here, be proxied?\nproxy_other: true\n# Optional dedicated Matrix service principal that consumes portal events through /sync.\n# When configured, the bridge bot ensures this user is invited to this bridge's portal rooms.\nmatrix_ingestor_mxid:\n''',
    )

    replace_once(
        connector,
        'import (\n\t"context"\n',
        'import (\n\t"context"\n\t"time"\n',
    )
    replace_once(
        connector,
        '''\terr = m.DB.Upgrade(ctx)\n\tif err != nil {\n\t\treturn bridgev2.DBUpgradeError{Err: err, Section: "meta"}\n\t}\n\treturn nil\n}\n''',
        '''\terr = m.DB.Upgrade(ctx)\n\tif err != nil {\n\t\treturn bridgev2.DBUpgradeError{Err: err, Section: "meta"}\n\t}\n\tif m.Config.MatrixIngestorMXID != "" && !m.Bridge.Background {\n\t\tgo m.matrixIngestorMembershipLoop(m.Bridge.BackgroundCtx)\n\t}\n\treturn nil\n}\n\nconst matrixIngestorReconcileInterval = 30 * time.Second\n\nfunc (m *MetaConnector) reconcileMatrixIngestorMembership(ctx context.Context) {\n\tportals, err := m.Bridge.GetAllPortalsWithMXID(ctx)\n\tif err != nil {\n\t\tm.Bridge.Log.Err(err).Msg("Failed to enumerate portal rooms for Matrix ingestor membership")\n\t\treturn\n\t}\n\tfor _, portal := range portals {\n\t\tif portal == nil || portal.MXID == "" {\n\t\t\tcontinue\n\t\t}\n\t\tif err = m.Bridge.Bot.EnsureInvited(ctx, portal.MXID, m.Config.MatrixIngestorMXID); err != nil {\n\t\t\tm.Bridge.Log.Err(err).Stringer("room_id", portal.MXID).Msg("Failed to ensure Matrix ingestor invitation")\n\t\t}\n\t}\n}\n\nfunc (m *MetaConnector) matrixIngestorMembershipLoop(ctx context.Context) {\n\tm.reconcileMatrixIngestorMembership(ctx)\n\tticker := time.NewTicker(matrixIngestorReconcileInterval)\n\tdefer ticker.Stop()\n\tfor {\n\t\tselect {\n\t\tcase <-ctx.Done():\n\t\t\treturn\n\t\tcase <-ticker.C:\n\t\t\tm.reconcileMatrixIngestorMembership(ctx)\n\t\t}\n\t}\n}\n''',
    )

    test_file = root / "pkg/connector/matrix_ingestor_membership_test.go"
    if test_file.exists():
        raise SystemExit(f"refusing to overwrite existing {test_file}")
    test_file.write_text('''package connector\n\nimport (\n\t"testing"\n\n\t"maunium.net/go/mautrix/id"\n)\n\nfunc TestMatrixIngestorMXIDValidation(t *testing.T) {\n\tvalid := &MetaConnector{Config: Config{MatrixIngestorMXID: id.UserID("@matrix-ingestor:matrix.example.com")}}\n\tif err := valid.ValidateConfig(); err != nil {\n\t\tt.Fatalf("valid Matrix ingestor MXID rejected: %v", err)\n\t}\n\tinvalid := &MetaConnector{Config: Config{MatrixIngestorMXID: id.UserID("not-an-mxid")}}\n\tif err := invalid.ValidateConfig(); err == nil {\n\t\tt.Fatal("invalid Matrix ingestor MXID was accepted")\n\t}\n}\n''')
    print(f"Applied Matrix ingestor membership patch to {root}")


if __name__ == "__main__":
    main()
