#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys


def replace_once(path: pathlib.Path, old: str, new: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one occurrence of {old!r}, found {count}")
    path.write_text(text.replace(old, new, 1))


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_matrix_ingestion_bridge.py <patched-mautrix-meta-source-dir>")
    root = pathlib.Path(sys.argv[1]).resolve()
    connector = root / "pkg/connector/connector.go"
    ingestion = root / "pkg/connector/matrix_ingestion.go"
    if ingestion.exists():
        raise SystemExit("matrix ingestion bridge file already exists")

    replace_once(
        connector,
        '''\terr = m.DB.Upgrade(ctx)\n\tif err != nil {\n\t\treturn bridgev2.DBUpgradeError{Err: err, Section: "meta"}\n\t}\n\treturn nil\n}\n''',
        '''\terr = m.DB.Upgrade(ctx)\n\tif err != nil {\n\t\treturn bridgev2.DBUpgradeError{Err: err, Section: "meta"}\n\t}\n\tif err = m.registerMatrixIngestionProvisioningRoute(); err != nil {\n\t\treturn err\n\t}\n\tif m.matrixIngestorMXID() != "" {\n\t\tgo m.reconcileMatrixIngestorMembership(m.Bridge.BackgroundCtx)\n\t}\n\treturn nil\n}\n''',
    )

    ingestion.write_text(r'''package connector

import (
    "context"
    "encoding/json"
    "fmt"
    "net/http"
    "os"
    "strings"
    "time"

    "maunium.net/go/mautrix/bridgev2"
    "maunium.net/go/mautrix/id"
)

const matrixIngestorReconcileInterval = 30 * time.Second

type matrixIngestionRoute struct {
    RoomID          id.RoomID `json:"room_id"`
    RemoteThreadID  string    `json:"remote_thread_id"`
    LoginID         string    `json:"login_id"`
    RemoteContactID string    `json:"remote_contact_id,omitempty"`
    SenderRemoteID  string    `json:"sender_remote_id,omitempty"`
}

func (m *MetaConnector) matrixIngestorMXID() id.UserID {
    raw := strings.TrimSpace(os.Getenv("MAUTRIX_META_INGESTOR_MXID"))
    if raw == "" {
        return ""
    }
    if !strings.HasPrefix(raw, "@") || !strings.Contains(raw, ":") || strings.ContainsAny(raw, " \t\r\n") {
        return ""
    }
    return id.UserID(raw)
}

func (m *MetaConnector) resolveMatrixIngestionRoute(ctx context.Context, roomID id.RoomID, senderMXID id.UserID) (*matrixIngestionRoute, error) {
    portal, err := m.Bridge.GetPortalByMXID(ctx, roomID)
    if err != nil {
        return nil, fmt.Errorf("failed to load portal: %w", err)
    }
    if portal == nil || portal.MXID == "" || portal.MXID != roomID || portal.ID == "" || portal.Receiver == "" {
        return nil, fmt.Errorf("portal attribution unavailable")
    }
    route := &matrixIngestionRoute{
        RoomID:          roomID,
        RemoteThreadID:  string(portal.ID),
        LoginID:         string(portal.Receiver),
        RemoteContactID: string(portal.OtherUserID),
    }
    if senderMXID != "" {
        remoteID, ok := m.Bridge.Matrix.ParseGhostMXID(senderMXID)
        if ok {
            route.SenderRemoteID = string(remoteID)
            if route.RemoteContactID == "" {
                route.RemoteContactID = string(remoteID)
            }
        }
    }
    return route, nil
}

func (m *MetaConnector) registerMatrixIngestionProvisioningRoute() error {
    matrixWithProvisioning, ok := m.Bridge.Matrix.(bridgev2.MatrixConnectorWithProvisioning)
    if !ok {
        return fmt.Errorf("matrix connector does not expose provisioning router")
    }
    provisioning := matrixWithProvisioning.GetProvisioning()
    if provisioning == nil || provisioning.GetRouter() == nil {
        return fmt.Errorf("matrix provisioning router is unavailable")
    }
    provisioning.GetRouter().HandleFunc("GET /v3/meta-stack/resolve-room/{roomID}", func(w http.ResponseWriter, r *http.Request) {
        roomID := id.RoomID(r.PathValue("roomID"))
        if roomID == "" {
            http.Error(w, "room ID is required", http.StatusBadRequest)
            return
        }
        senderMXID := id.UserID(r.URL.Query().Get("sender"))
        route, err := m.resolveMatrixIngestionRoute(r.Context(), roomID, senderMXID)
        if err != nil {
            http.Error(w, "room attribution unavailable", http.StatusNotFound)
            return
        }
        w.Header().Set("Content-Type", "application/json")
        if err = json.NewEncoder(w).Encode(route); err != nil {
            zerolog.Ctx(r.Context()).Err(err).Msg("Failed to encode matrix ingestion route")
        }
    })
    return nil
}

func (m *MetaConnector) reconcileMatrixIngestorMembership(ctx context.Context) {
    mxid := m.matrixIngestorMXID()
    if mxid == "" {
        return
    }
    reconcile := func() {
        portals, err := m.Bridge.GetAllPortalsWithMXID(ctx)
        if err != nil {
            m.Bridge.Log.Err(err).Msg("Failed to list portals for matrix ingestor reconciliation")
            return
        }
        for _, portal := range portals {
            if portal == nil || portal.MXID == "" {
                continue
            }
            if err = m.Bridge.Bot.EnsureInvited(ctx, portal.MXID, mxid); err != nil {
                m.Bridge.Log.Warn().Err(err).Stringer("room_id", portal.MXID).Stringer("ingestor_mxid", mxid).Msg("Failed to invite matrix ingestor to portal")
            }
        }
    }
    reconcile()
    ticker := time.NewTicker(matrixIngestorReconcileInterval)
    defer ticker.Stop()
    for {
        select {
        case <-ctx.Done():
            return
        case <-ticker.C:
            reconcile()
        }
    }
}
'''.replace('"maunium.net/go/mautrix/id"\n)', '"maunium.net/go/mautrix/id"\n    "github.com/rs/zerolog"\n)'))


if __name__ == "__main__":
    main()
