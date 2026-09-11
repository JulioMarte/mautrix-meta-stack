#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys


def replace_once(path: pathlib.Path, old: str, new: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one fragment, found {count}")
    path.write_text(text.replace(old, new, 1))


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_matrix_ingestion_metadata.py <patched-mautrix-meta-source-dir>")
    root = pathlib.Path(sys.argv[1]).resolve()
    events = root / "pkg/connector/events.go"
    if not events.is_file():
        raise SystemExit(f"missing connector events file: {events}")

    replace_once(
        events,
        "type FBMessageEvent struct {\n",
        '''const matrixRemoteSenderIDKey = "com.mautrix_meta_stack.remote_sender_id"\nconst matrixIngestionProvenanceKey = "com.mautrix_meta_stack.provenance"\n\nfunc annotateMatrixRemoteEvent(message *bridgev2.ConvertedMessage, senderID string) *bridgev2.ConvertedMessage {\n\tif message == nil {\n\t\treturn nil\n\t}\n\tfor _, part := range message.Parts {\n\t\tif part == nil {\n\t\t\tcontinue\n\t\t}\n\t\tif part.Extra == nil {\n\t\t\tpart.Extra = make(map[string]any)\n\t\t}\n\t\tpart.Extra[matrixIngestionProvenanceKey] = map[string]any{"source": "meta"}\n\t\tif senderID != "" {\n\t\t\tpart.Extra[matrixRemoteSenderIDKey] = senderID\n\t\t}\n\t}\n\treturn message\n}\n\ntype FBMessageEvent struct {\n''',
    )

    replace_once(
        events,
        '''\treturn evt.m.Main.MsgConv.ToMatrix(ctx, portal, cli, evt.m.UserLogin, intent, evt.GetID(), evt.WrappedMessage, evt.m.Main.Config.DisableXMAAlways), nil\n''',
        '''\tconverted := evt.m.Main.MsgConv.ToMatrix(ctx, portal, cli, evt.m.UserLogin, intent, evt.GetID(), evt.WrappedMessage, evt.m.Main.Config.DisableXMAAlways)\n\treturn annotateMatrixRemoteEvent(converted, fmt.Sprint(evt.SenderId)), nil\n''',
    )

    replace_once(
        events,
        '''\treturn evt.m.Main.MsgConv.WhatsAppToMatrix(ctx, portal, evt.m.Client, evt.m.E2EEClient, evt.m.UserLogin, intent, evt.GetID(), evt.FBMessage), nil\n''',
        '''\tconverted := evt.m.Main.MsgConv.WhatsAppToMatrix(ctx, portal, evt.m.Client, evt.m.E2EEClient, evt.m.UserLogin, intent, evt.GetID(), evt.FBMessage)\n\treturn annotateMatrixRemoteEvent(converted, fmt.Sprint(evt.Info.Sender.UserInt())), nil\n''',
    )

    test_file = root / "pkg/connector/matrix_ingestion_metadata_test.go"
    if test_file.exists():
        raise SystemExit(f"refusing to overwrite existing {test_file}")
    test_file.write_text('''package connector\n\nimport (\n\t"testing"\n\n\t"maunium.net/go/mautrix/bridgev2"\n)\n\nfunc TestAnnotateMatrixRemoteEvent(t *testing.T) {\n\tmessage := &bridgev2.ConvertedMessage{Parts: []*bridgev2.ConvertedMessagePart{\n\t\t{Extra: map[string]any{"existing": "kept"}},\n\t\t{},\n\t\tnil,\n\t}}\n\tgot := annotateMatrixRemoteEvent(message, "123456")\n\tif got != message {\n\t\tt.Fatal("annotation must preserve converted message identity")\n\t}\n\tfor index, part := range message.Parts[:2] {\n\t\tif part.Extra[matrixRemoteSenderIDKey] != "123456" {\n\t\t\tt.Fatalf("part %d missing remote sender annotation: %#v", index, part.Extra)\n\t\t}\n\t\tprovenance, ok := part.Extra[matrixIngestionProvenanceKey].(map[string]any)\n\t\tif !ok || provenance["source"] != "meta" {\n\t\t\tt.Fatalf("part %d missing Meta provenance: %#v", index, part.Extra)\n\t\t}\n\t}\n\tif message.Parts[0].Extra["existing"] != "kept" {\n\t\tt.Fatalf("existing metadata was overwritten: %#v", message.Parts[0].Extra)\n\t}\n}\n\nfunc TestAnnotateMatrixRemoteEventKeepsProvenanceWithoutSender(t *testing.T) {\n\tmessage := &bridgev2.ConvertedMessage{Parts: []*bridgev2.ConvertedMessagePart{{}}}\n\tannotateMatrixRemoteEvent(message, "")\n\tif _, ok := message.Parts[0].Extra[matrixRemoteSenderIDKey]; ok {\n\t\tt.Fatalf("empty sender unexpectedly emitted metadata: %#v", message.Parts[0].Extra)\n\t}\n\tprovenance := message.Parts[0].Extra[matrixIngestionProvenanceKey].(map[string]any)\n\tif provenance["source"] != "meta" {\n\t\tt.Fatalf("Meta provenance missing: %#v", message.Parts[0].Extra)\n\t}\n}\n''')
    print(f"Applied Matrix ingestion event metadata patch to {root}")


if __name__ == "__main__":
    main()
