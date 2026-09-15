"""Media bridging, authoritative Meta history direction, and Chatwoot context sync.

This layer intentionally reads mautrix-meta's bridgev2 SQLite database read-only. The
integration already mounts the mautrix data volume read-only, so this gives us two
facts Matrix events alone cannot provide reliably:

* whether a historical remote message was sent by the logged-in Meta account, and
* whether a portal is a Facebook Marketplace thread.

Those facts are used to mirror history with the correct Chatwoot direction and to
apply Marketplace labels/custom attributes to both new and already-linked
conversations. Media is copied between Matrix and Chatwoot; URLs from Chatwoot are
accepted only after the callback itself or the exact message has been authenticated.
"""
from __future__ import annotations

import json
import mimetypes
import os
import sqlite3
import time
from urllib.parse import quote, urlparse

import requests

import final_app as runtime
import runtime_enhancements as enhancements
import delivery_history_v2 as delivery

legacy = runtime.legacy
prod = runtime.prod

META_DB_PATH = os.getenv("MAUTRIX_META_DB_PATH", "/mautrix/mautrix-meta.db")
MARKETPLACE_THREAD_TYPE = 5
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
MIRROR_MARKER = "matrix_bridge_mirror"
SUPPORTED_MEDIA_MSGTYPES = {"m.image", "m.video", "m.audio", "m.file", "m.sticker"}


def _meta_db():
    return sqlite3.connect(f"file:{META_DB_PATH}?mode=ro", uri=True, timeout=5)


def _json_object(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _self_remote_ids(conn) -> set[str]:
    rows = conn.execute(
        "SELECT id FROM user_login WHERE user_mxid = ?",
        (legacy.MATRIX_ADMIN_MXID,),
    ).fetchall()
    ids = {str(row[0]) for row in rows if row and row[0] is not None}
    if ids:
        return ids
    # This stack is deliberately single-client. Keep a compatibility fallback for
    # databases created before user_mxid normalization changed.
    rows = conn.execute("SELECT id FROM user_login LIMIT 2").fetchall()
    if len(rows) == 1 and rows[0][0] is not None:
        return {str(rows[0][0])}
    return set()


def portal_context(room_id: str) -> dict:
    try:
        with _meta_db() as conn:
            row = conn.execute(
                "SELECT id, receiver, parent_id, name, metadata FROM portal WHERE mxid = ? LIMIT 1",
                (room_id,),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return {}
    if not row:
        return {}
    metadata = _json_object(row[4])
    try:
        thread_type = int(metadata.get("thread_type"))
    except (TypeError, ValueError):
        thread_type = None
    return {
        "portal_id": str(row[0] or ""),
        "receiver": str(row[1] or ""),
        "parent_id": str(row[2] or ""),
        "name": str(row[3] or ""),
        "thread_type": thread_type,
        "is_marketplace": thread_type == MARKETPLACE_THREAD_TYPE,
    }


def meta_message_record(event_id: str) -> dict:
    if not event_id:
        return {}
    try:
        with _meta_db() as conn:
            row = conn.execute(
                "SELECT sender_id, sender_mxid, room_id, room_receiver, timestamp "
                "FROM message WHERE mxid = ? ORDER BY rowid DESC LIMIT 1",
                (event_id,),
            ).fetchone()
            if not row:
                return {}
            self_ids = _self_remote_ids(conn)
    except (OSError, sqlite3.Error):
        return {}
    sender_id = str(row[0] or "")
    return {
        "sender_id": sender_id,
        "sender_mxid": str(row[1] or ""),
        "room_id": str(row[2] or ""),
        "room_receiver": str(row[3] or ""),
        "timestamp": int(row[4] or 0),
        "from_me": bool(sender_id and sender_id in self_ids),
    }


def message_direction(event: dict) -> str:
    sender = str(event.get("sender") or "")
    if sender == legacy.MATRIX_ADMIN_MXID:
        return "outgoing"
    record = meta_message_record(str(event.get("event_id") or ""))
    if record:
        return "outgoing" if record.get("from_me") else "incoming"
    return "incoming"


def _customer_sender_for_room(room_id: str) -> str:
    try:
        with _meta_db() as conn:
            self_ids = _self_remote_ids(conn)
            portal = conn.execute(
                "SELECT bridge_id, id, receiver FROM portal WHERE mxid = ? LIMIT 1",
                (room_id,),
            ).fetchone()
            if not portal:
                return ""
            sql = (
                "SELECT sender_id, sender_mxid FROM message "
                "WHERE bridge_id = ? AND room_id = ? AND room_receiver = ? "
            )
            params: list[object] = [portal[0], portal[1], portal[2]]
            if self_ids:
                placeholders = ",".join("?" for _ in self_ids)
                sql += f"AND sender_id NOT IN ({placeholders}) "
                params.extend(sorted(self_ids))
            sql += "ORDER BY timestamp DESC, rowid DESC LIMIT 1"
            row = conn.execute(sql, params).fetchone()
    except (OSError, sqlite3.Error):
        return ""
    if not row:
        return ""
    sender_mxid = str(row[1] or "")
    if sender_mxid:
        return sender_mxid
    sender_id = str(row[0] or "")
    return f"@meta_{sender_id}:{legacy.MATRIX_SERVER_NAME}" if sender_id else ""


def _processed_direction(event_id: str) -> str:
    if not event_id:
        return ""
    with legacy.db() as conn:
        row = conn.execute(
            "SELECT direction FROM processed_events WHERE event_id = ?", (event_id,)
        ).fetchone()
    return str(row["direction"] or "") if row else ""


def _cw_json(method: str, path: str, payload: dict | None = None):
    headers = legacy.chatwoot_headers()
    response = requests.request(
        method,
        legacy.chatwoot_url(path),
        headers=headers,
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    return response.json() if response.content else {}


def sync_conversation_context(room_id: str, link) -> None:
    context = portal_context(room_id)
    if not context:
        return
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    conversation_id = int(link["conversation_id"])
    desired_attributes = {
        "matrix_room_id": room_id,
        "meta_network": "facebook",
        "meta_portal_id": context.get("portal_id") or "",
        "meta_thread_type": context.get("thread_type"),
        "meta_channel": "facebook_marketplace" if context.get("is_marketplace") else "facebook_messenger",
    }
    if context.get("parent_id"):
        desired_attributes["meta_parent_portal_id"] = context["parent_id"]

    conversation = prod.cw_get(
        f"/api/v1/accounts/{account_id}/conversations/{conversation_id}"
    )
    current_attrs = conversation.get("custom_attributes") if isinstance(conversation, dict) else {}
    merged_attrs = dict(current_attrs) if isinstance(current_attrs, dict) else {}
    merged_attrs.update({k: v for k, v in desired_attributes.items() if v is not None})
    if merged_attrs != current_attrs:
        _cw_json(
            "POST",
            f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/custom_attributes",
            {"custom_attributes": merged_attrs},
        )

    labels_data = prod.cw_get(
        f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/labels"
    )
    labels = labels_data.get("payload") if isinstance(labels_data, dict) else []
    current_labels = {str(item) for item in labels or [] if item}
    desired_labels = {"facebook"}
    if context.get("is_marketplace"):
        desired_labels.add("marketplace")
    merged_labels = sorted(current_labels | desired_labels)
    if set(merged_labels) != current_labels:
        _cw_json(
            "POST",
            f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/labels",
            {"labels": merged_labels},
        )


def _mxc_parts(mxc: str) -> tuple[str, str]:
    if not mxc.startswith("mxc://"):
        raise RuntimeError("Matrix attachment does not contain a valid mxc URI")
    rest = mxc[6:]
    if "/" not in rest:
        raise RuntimeError("Matrix attachment contains an invalid mxc URI")
    server, media_id = rest.split("/", 1)
    if not server or not media_id:
        raise RuntimeError("Matrix attachment contains an invalid mxc URI")
    return server, media_id


def _bounded_download(url: str, *, headers: dict | None = None) -> tuple[bytes, str]:
    response = requests.get(url, headers=headers or {}, stream=True, timeout=30)
    response.raise_for_status()
    length = response.headers.get("Content-Length")
    if length:
        try:
            if int(length) > MAX_ATTACHMENT_BYTES:
                raise RuntimeError("Attachment exceeds the 50 MiB integration limit")
        except ValueError:
            pass
    chunks = []
    total = 0
    for chunk in response.iter_content(1024 * 256):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_ATTACHMENT_BYTES:
            raise RuntimeError("Attachment exceeds the 50 MiB integration limit")
        chunks.append(chunk)
    return b"".join(chunks), str(response.headers.get("Content-Type") or "").split(";", 1)[0]


def download_matrix_media(content: dict) -> tuple[bytes, str, str, str]:
    mxc = str(content.get("url") or "")
    if not mxc and isinstance(content.get("file"), dict):
        raise RuntimeError("Encrypted Matrix media is not supported by this unencrypted stack")
    server, media_id = _mxc_parts(mxc)
    url = (
        f"{legacy.MATRIX_HOMESERVER}/_matrix/client/v1/media/download/"
        f"{quote(server, safe='')}/{quote(media_id, safe='')}"
    )
    data, response_mime = _bounded_download(url, headers=legacy.matrix_headers())
    info = content.get("info") or {}
    mimetype = str(info.get("mimetype") or response_mime or "application/octet-stream")
    filename = str(content.get("filename") or content.get("body") or "attachment").strip() or "attachment"
    body = str(content.get("body") or "").strip()
    caption = body if body and body != filename else ""
    return data, filename, mimetype, caption


def post_chatwoot_media(conversation_id: int, *, data: bytes, filename: str, mimetype: str,
                        caption: str, direction: str, event_id: str) -> dict:
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    form = {
        "content": caption,
        "message_type": direction,
        "private": "false",
        "content_type": "text",
        "content_attributes": json.dumps({MIRROR_MARKER: True, "matrix_event_id": event_id}),
    }
    response = requests.post(
        legacy.chatwoot_url(
            f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
        ),
        headers={"api_access_token": legacy.get_setting("chatwoot_api_token")},
        data=form,
        files={"attachments[]": (filename, data, mimetype)},
        timeout=45,
    )
    response.raise_for_status()
    return response.json() if response.content else {}


def _post_chatwoot_text(conversation_id: int, *, body: str, direction: str,
                        event_id: str, history: bool) -> dict:
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    attributes = {MIRROR_MARKER: True, "matrix_event_id": event_id}
    if history:
        attributes[delivery.HISTORY_MARKER] = True
    return legacy.cw_post(
        f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages",
        {
            "content": body,
            "message_type": direction,
            "private": False,
            "content_type": "text",
            "content_attributes": attributes,
        },
    )


def mirror_matrix_event(room_id: str, event: dict, *, history: bool = False) -> bool:
    if not legacy.configured() or event.get("type") != "m.room.message":
        return False
    event_id = str(event.get("event_id") or "")
    if not event_id or legacy.event_seen(event_id):
        return False
    if not prod.is_bridge_portal(room_id):
        return False

    sender = str(event.get("sender") or "")
    if sender == legacy.bridge_bot_mxid():
        return False
    direction = message_direction(event)
    if not history and sender == legacy.MATRIX_ADMIN_MXID:
        # Chatwoot-originated replies already exist in Chatwoot; the live Matrix
        # echo must never be mirrored back as a second outgoing Chatwoot message.
        return False

    if direction == "incoming":
        customer_sender = sender
    else:
        customer_sender = _customer_sender_for_room(room_id)
    if not customer_sender:
        return False

    link = prod.ensure_room_link(room_id, customer_sender)
    sync_conversation_context(room_id, link)
    content = event.get("content") or {}
    msgtype = str(content.get("msgtype") or "")

    if msgtype in ("m.text", "m.notice"):
        body = str(content.get("body") or "").strip()
        if not body:
            return False
        _post_chatwoot_text(
            int(link["conversation_id"]), body=body, direction=direction,
            event_id=event_id, history=history,
        )
    elif msgtype in SUPPORTED_MEDIA_MSGTYPES:
        data, filename, mimetype, caption = download_matrix_media(content)
        post_chatwoot_media(
            int(link["conversation_id"]), data=data, filename=filename,
            mimetype=mimetype, caption=caption, direction=direction, event_id=event_id,
        )
    else:
        return False

    legacy.mark_event(
        event_id,
        "matrix_history_outgoing_to_chatwoot" if history and direction == "outgoing" else "matrix_to_chatwoot",
    )
    return True


def _activation_allows(event: dict) -> bool:
    try:
        cutoff = int(legacy.get_setting("chatwoot_enabled_at_ms", "0") or 0)
        event_ts = int(event.get("origin_server_ts") or 0)
    except (TypeError, ValueError):
        return True
    return not (cutoff and event_ts and event_ts < cutoff)


def live_matrix_event(room_id: str, event: dict):
    if event.get("type") == "m.room.message":
        enhancements.repair_deleted_conversation(room_id)
        if not _activation_allows(event):
            return None
        existing_id = runtime._linked_conversation_id(room_id)
        if existing_id is not None:
            matches, conversation_id, actual_inbox_id = runtime.room_matches_configured_chatwoot_inbox(room_id)
            if not matches:
                configured_inbox = int(legacy.get_setting("chatwoot_inbox_id"))
                runtime._unlink_room_conversation(room_id, conversation_id)
                print(
                    "matrix conversation relinked to configured inbox "
                    f"old_conversation={conversation_id} actual_inbox={actual_inbox_id} "
                    f"configured_inbox={configured_inbox}",
                    flush=True,
                )
    return mirror_matrix_event(room_id, event, history=False)


def import_recent_history(room_id: str) -> int:
    if not enhancements.setting_bool("import_history_on_join", True):
        return 0
    days = delivery.history_days()
    if days <= 0:
        return 0

    # Context sync happens even when every event was already imported. This is what
    # propagates Marketplace labels/custom attributes to pre-existing conversations.
    with legacy.db() as conn:
        existing = conn.execute("SELECT * FROM room_links WHERE room_id = ?", (room_id,)).fetchone()
    if existing:
        sync_conversation_context(room_id, existing)

    cutoff_ms = int((time.time() - days * 86400) * 1000)
    path = f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/messages"
    token = ""
    collected: list[dict] = []
    while True:
        params = {"dir": "b", "limit": delivery.MATRIX_PAGE_SIZE}
        if token:
            params["from"] = token
        response = enhancements._matrix_get(path, params=params)
        payload = response.json() or {}
        chunk = [item for item in (payload.get("chunk") or []) if isinstance(item, dict)]
        if not chunk:
            break
        crossed = False
        for event in chunk:
            try:
                event_ts = int(event.get("origin_server_ts") or 0)
            except (TypeError, ValueError):
                event_ts = 0
            if event_ts and event_ts < cutoff_ms:
                crossed = True
                continue
            if event.get("type") == "m.room.message":
                collected.append(event)
        next_token = str(payload.get("end") or payload.get("end_token") or "")
        if crossed or not next_token or next_token == token:
            break
        token = next_token

    imported = 0
    repair_needed = 0
    try:
        enabled_at = int(legacy.get_setting("chatwoot_enabled_at_ms", "0") or 0)
    except ValueError:
        enabled_at = 0
    for event in reversed(collected):
        event_id = str(event.get("event_id") or "")
        if legacy.event_seen(event_id):
            if message_direction(event) == "outgoing" and _processed_direction(event_id) == "matrix_to_chatwoot":
                repair_needed += 1
            continue
        sender = str(event.get("sender") or "")
        try:
            event_ts = int(event.get("origin_server_ts") or 0)
        except (TypeError, ValueError):
            event_ts = 0
        if sender == legacy.MATRIX_ADMIN_MXID and enabled_at and event_ts >= enabled_at:
            # These are normally echoes of already-existing Chatwoot replies from the
            # Chatwoot-only era. Importing them again would duplicate agent messages.
            legacy.mark_event(event_id, "post_activation_admin_history_skipped")
            continue
        try:
            if mirror_matrix_event(room_id, event, history=True):
                imported += 1
        except Exception as exc:
            print(
                f"history import failed room={room_id} event={event_id}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            continue

    legacy.set_setting("historical_direction_repair_needed", str(repair_needed))
    if repair_needed:
        print(
            f"historical direction repair needed room={room_id} count={repair_needed}; "
            "old Chatwoot rows predate Matrix-event mapping and were left untouched",
            flush=True,
        )
    return imported


def _attachment_url(attachment: dict) -> str:
    for key in ("data_url", "download_url", "file_url", "url"):
        value = str(attachment.get(key) or "").strip()
        if value:
            if value.startswith("/"):
                return legacy.get_setting("chatwoot_base_url").rstrip("/") + value
            parsed = urlparse(value)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                return value
    raise RuntimeError("Chatwoot attachment has no downloadable URL")


def _attachment_filename(attachment: dict, index: int) -> str:
    meta = attachment.get("meta") if isinstance(attachment.get("meta"), dict) else {}
    for value in (
        attachment.get("file_name"), attachment.get("filename"), attachment.get("fallback_title"),
        meta.get("original_filename"), meta.get("filename"),
    ):
        if value:
            return str(value)
    extension = str(attachment.get("extension") or "").lstrip(".")
    return f"attachment-{index}.{extension}" if extension else f"attachment-{index}"


def _attachment_mimetype(attachment: dict, filename: str, response_mime: str) -> str:
    meta = attachment.get("meta") if isinstance(attachment.get("meta"), dict) else {}
    for value in (attachment.get("content_type"), meta.get("content_type"), meta.get("mimetype")):
        if value and "/" in str(value):
            return str(value)
    guessed = mimetypes.guess_type(filename)[0]
    return guessed or response_mime or "application/octet-stream"


def _msgtype_for_mime(mimetype: str) -> str:
    if mimetype.startswith("image/"):
        return "m.image"
    if mimetype.startswith("video/"):
        return "m.video"
    if mimetype.startswith("audio/"):
        return "m.audio"
    return "m.file"


def _matrix_upload(data: bytes, filename: str, mimetype: str) -> str:
    response = requests.post(
        f"{legacy.MATRIX_HOMESERVER}/_matrix/media/v3/upload",
        headers={**legacy.matrix_headers(), "Content-Type": mimetype},
        params={"filename": filename},
        data=data,
        timeout=45,
    )
    response.raise_for_status()
    content_uri = str((response.json() or {}).get("content_uri") or "")
    if not content_uri.startswith("mxc://"):
        raise RuntimeError("Matrix media upload did not return an mxc URI")
    return content_uri


def _matrix_send(room_id: str, txn_id: str, content: dict) -> str:
    encoded_room = quote(room_id, safe="")
    encoded_txn = quote(txn_id, safe="")
    response = requests.put(
        f"{legacy.MATRIX_HOMESERVER}/_matrix/client/v3/rooms/{encoded_room}/send/m.room.message/{encoded_txn}",
        headers=legacy.matrix_headers(),
        json=content,
        timeout=20,
    )
    response.raise_for_status()
    event_id = str((response.json() or {}).get("event_id") or "")
    if not event_id:
        raise RuntimeError("Matrix did not acknowledge the outbound event")
    return event_id


def _verify_matrix_content(room_id: str, event_id: str, expected: dict) -> None:
    encoded_room = quote(room_id, safe="")
    encoded_event = quote(event_id, safe="")
    response = requests.get(
        f"{legacy.MATRIX_HOMESERVER}/_matrix/client/v3/rooms/{encoded_room}/event/{encoded_event}",
        headers=legacy.matrix_headers(),
        timeout=20,
    )
    response.raise_for_status()
    event = response.json() if response.content else {}
    actual = event.get("content") or {}
    for key in ("msgtype", "body", "url"):
        if key in expected and actual.get(key) != expected.get(key):
            raise RuntimeError(f"Matrix acknowledged outbound event with unexpected {key}")


def _authenticated_attachments(payload: dict, *, signature_verified: bool) -> tuple[list[dict], dict | None]:
    callback_attachments = [item for item in (payload.get("attachments") or []) if isinstance(item, dict)]
    if signature_verified:
        return callback_attachments, None
    authenticated = delivery.verify_outgoing_against_chatwoot(payload)
    attachments = [item for item in (authenticated.get("attachments") or []) if isinstance(item, dict)]
    return attachments, authenticated


def handle_chatwoot_outgoing(payload: dict, *, signature_verified: bool) -> dict:
    if payload.get("event") != "message_created":
        return {"ok": True, "ignored": True}
    if not delivery._message_type_is_outgoing(payload.get("message_type")) or payload.get("private") is True:
        return {"ok": True, "ignored": True}
    if not delivery._configured_inbox_matches(payload):
        return {"ok": True, "ignored": True, "reason": "outside_configured_chatwoot_inbox"}

    attributes = payload.get("content_attributes") or {}
    if isinstance(attributes, dict) and (
        attributes.get(delivery.HISTORY_MARKER) is True or attributes.get(MIRROR_MARKER) is True
    ):
        return {"ok": True, "ignored": True, "reason": "matrix_mirror"}

    conversation_id = delivery._conversation_id(payload)
    message_id = str(payload.get("id") or "")
    content = str(payload.get("content") or "").strip()
    if not message_id:
        return {"ok": True, "ignored": True, "reason": "missing_message_id"}
    event_key = "chatwoot:" + message_id
    if legacy.event_seen(event_key):
        return {"ok": True, "duplicate": True}

    attachments, authenticated = _authenticated_attachments(payload, signature_verified=signature_verified)
    if authenticated is not None:
        content = str(authenticated.get("content") or "").strip()
    if not content and not attachments:
        return {"ok": True, "ignored": True, "reason": "empty_message"}

    with legacy.db() as conn:
        link = conn.execute(
            "SELECT * FROM room_links WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
    if not link:
        raise RuntimeError(f"Chatwoot conversation {conversation_id} has no Matrix room mapping")

    matches, _, actual_inbox = runtime.room_matches_configured_chatwoot_inbox(link["room_id"])
    if not matches:
        raise RuntimeError(f"Chatwoot conversation moved outside configured inbox ({actual_inbox})")

    event_ids: list[str] = []
    if content:
        text_event = {
            "msgtype": "m.text",
            "body": content,
        }
        event_id = _matrix_send(link["room_id"], f"cw-{message_id}-text", text_event)
        _verify_matrix_content(link["room_id"], event_id, text_event)
        legacy.mark_event(event_id, "chatwoot_to_matrix_origin")
        event_ids.append(event_id)

    for index, attachment in enumerate(attachments, start=1):
        url = _attachment_url(attachment)
        data, response_mime = _bounded_download(
            url,
            headers={"api_access_token": legacy.get_setting("chatwoot_api_token")},
        )
        filename = _attachment_filename(attachment, index)
        mimetype = _attachment_mimetype(attachment, filename, response_mime)
        mxc = _matrix_upload(data, filename, mimetype)
        media_event = {
            "msgtype": _msgtype_for_mime(mimetype),
            "body": filename,
            "url": mxc,
            "info": {"mimetype": mimetype, "size": len(data)},
        }
        event_id = _matrix_send(link["room_id"], f"cw-{message_id}-att-{index}", media_event)
        _verify_matrix_content(link["room_id"], event_id, media_event)
        legacy.mark_event(event_id, "chatwoot_to_matrix_origin")
        event_ids.append(event_id)

    legacy.mark_event(event_key, "chatwoot_to_matrix")
    now = delivery._now_utc()
    legacy.set_setting("api_inbox_delivery_verified_at", now)
    legacy.set_setting("last_chatwoot_matrix_delivery_at", now)
    legacy.set_setting("last_chatwoot_matrix_event_id", event_ids[-1] if event_ids else "")
    legacy.set_setting("last_chatwoot_matrix_conversation_id", str(conversation_id))
    legacy.set_setting("last_chatwoot_matrix_error", "")
    print(
        f"Chatwoot outgoing verified in Matrix conversation={conversation_id} "
        f"room={link['room_id']} chatwoot_message={message_id} matrix_events={','.join(event_ids)}",
        flush=True,
    )
    return {"ok": True, "matrix_event_ids": event_ids, "matrix_event_id": event_ids[-1] if event_ids else ""}


def install() -> None:
    # The sync loop resolves enhancements.enhanced_live_matrix_event at runtime, so
    # patching it here replaces the text-only handler without starting a second sync.
    enhancements.enhanced_live_matrix_event = live_matrix_event
    enhancements.import_recent_history = import_recent_history
    prod.matrix_event_to_chatwoot = mirror_matrix_event
    legacy.matrix_event_to_chatwoot = live_matrix_event
    delivery.handle_chatwoot_outgoing_verified = handle_chatwoot_outgoing
