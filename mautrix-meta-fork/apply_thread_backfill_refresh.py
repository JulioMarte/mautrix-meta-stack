#!/usr/bin/env python3
"""Guarantee initial thread-list pagination even when Meta Ready is not emitted.

Upstream starts StartThreadBackfill only from the Messagix Ready event. We have
observed successful initial-table processing and an established main stream without
that event reaching the connector, leaving only the first page of conversations.
Keep upstream's BackfillCompleted semantics, but add a guarded delayed fallback
after Connect succeeds. A per-login mutex prevents the normal Ready path and the
fallback from running the pagination concurrently.
"""
from __future__ import annotations

import pathlib
import sys

root = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(".")
client_path = root / "pkg/connector/client.go"
backfill_path = root / "pkg/connector/threadbackfill.go"

client = client_path.read_text(encoding="utf-8")
field_old = "\tbackfillLock        sync.Mutex\n"
field_new = "\tbackfillLock        sync.Mutex\n\tthreadBackfillLock  sync.Mutex\n"
if field_old not in client:
    raise SystemExit("expected MetaClient backfillLock field not found")
client = client.replace(field_old, field_new, 1)

connect_old = '''\terr = m.Client.Connect(ctx)\n\tif err != nil {\n\t\tzerolog.Ctx(ctx).Err(err).Msg("Failed to connect")\n\t\tm.UserLogin.BridgeState.Send(status.BridgeState{\n\t\t\tStateEvent: status.StateUnknownError,\n\t\t\tError:      MetaConnectError,\n\t\t})\n\t\treturn\n\t}\n\n\tgo m.periodicReconnect()\n'''
connect_new = '''\terr = m.Client.Connect(ctx)\n\tif err != nil {\n\t\tzerolog.Ctx(ctx).Err(err).Msg("Failed to connect")\n\t\tm.UserLogin.BridgeState.Send(status.BridgeState{\n\t\t\tStateEvent: status.StateUnknownError,\n\t\t\tError:      MetaConnectError,\n\t\t})\n\t\treturn\n\t}\n\n\t// Ready normally starts thread backfill. Some successful Facebook sessions\n\t// establish the socket and process the initial table without delivering Ready,\n\t// which leaves only the first page of conversations. Give the normal path time\n\t// to run, then start the same idempotent backfill as a fallback.\n\tgo func() {\n\t\tselect {\n\t\tcase <-time.After(5 * time.Second):\n\t\tcase <-ctx.Done():\n\t\t\treturn\n\t\t}\n\t\tif m.LoginMeta.BackfillCompleted {\n\t\t\treturn\n\t\t}\n\t\tzerolog.Ctx(ctx).Info().Msg("Starting thread backfill from connect watchdog")\n\t\tif err := m.StartThreadBackfill(ctx); err != nil {\n\t\t\tzerolog.Ctx(ctx).Err(err).Msg("Thread backfill watchdog failed")\n\t\t}\n\t}()\n\n\tgo m.periodicReconnect()\n'''
if connect_old not in client:
    raise SystemExit("expected connectWithTable tail not found")
client_path.write_text(client.replace(connect_old, connect_new, 1), encoding="utf-8")

backfill = backfill_path.read_text(encoding="utf-8")
start_old = '''func (m *MetaClient) StartThreadBackfill(ctx context.Context) error {\n\tif m.Main.Config.ThreadBackfill.BatchCount == 0 {\n\t\treturn nil\n\t}\n\n\tlog := m.UserLogin.Log.With().Str("action", "thread_backfill").Logger()\n'''
start_new = '''func (m *MetaClient) StartThreadBackfill(ctx context.Context) error {\n\tif m.Main.Config.ThreadBackfill.BatchCount == 0 {\n\t\treturn nil\n\t}\n\tif !m.threadBackfillLock.TryLock() {\n\t\treturn nil\n\t}\n\tdefer m.threadBackfillLock.Unlock()\n\n\tlog := m.UserLogin.Log.With().Str("action", "thread_backfill").Logger()\n'''
if start_old not in backfill:
    raise SystemExit("expected StartThreadBackfill header not found")
backfill_path.write_text(backfill.replace(start_old, start_new, 1), encoding="utf-8")
