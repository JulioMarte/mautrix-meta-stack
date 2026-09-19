"""Generation-scoped Meta -> Chatwoot identity and self-healing bindings.

Installed last by runtime_entrypoint. mautrix-meta's persisted portal identity is
authoritative; Matrix rooms and Chatwoot conversations are projections.
"""
from __future__ import annotations

import contextvars
import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass

import requests

import conversation_lifecycle_v10 as lifecycle
import final_app as runtime
import marketplace_rebuild_v4 as rebuild
import media_context_v3 as media
import meta_portal_reconcile as portal_reconcile
import nicegui_app
import runtime_enhancements as enhancements

legacy = runtime.legacy
prod = runtime.prod

VERIFY_TTL_SECONDS = 300
PROFILE_SYNC_VERSION = 1
PROFILE_RECONCILE_TTL_SECONDS = 900
RUNTIME_RECONCILE_VERSION = "profile-reconcile-v1"
_LOCK = lifecycle._RECONCILE_LOCK
_CREATE_LOCKS_GUARD = threading.Lock()
_CREATE_LOCKS = {}
_VALIDATED_TARGET = contextvars.ContextVar("validated_binding_target", default=None)
_INSTALLED = False

_base_save_configuration = None
_base_event_seen = legacy.event_seen
_base_mark_event = legacy.mark_event
_base_post_text = None
_base_post_media = None
_base_context_sync = None
_base_mirror = None
_base_reconcile = None
_base_import_history = None
_base_lifecycle_start = None
_base_lifecycle_delete = None


class IntentionalConversationDeletion(RuntimeError):
    pass


class BindingChanged(RuntimeError):
    pass


@dataclass(frozen=True)
class BindingTarget:
    base_url: str
    account_id: int
    inbox_id: int
    inbox_identifier: str = ""
    channel_type: str = ""

    @property
    def key(self):
        return (self.base_url.rstrip("/"), int(self.account_id), int(self.inbox_id))


def _now():
    return int(time.time())


def _ensure_column(conn, table, column, ddl):
    cols = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def ensure_schema():
    with legacy.db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS integrations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              provider TEXT NOT NULL,
              scope_key TEXT NOT NULL UNIQUE,
              remote_account_id TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'ACTIVE',
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS external_identities (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              integration_id INTEGER NOT NULL,
              bridge_id TEXT NOT NULL,
              portal_id TEXT NOT NULL,
              portal_receiver TEXT NOT NULL DEFAULT '',
              matrix_room_id TEXT NOT NULL,
              parent_portal_id TEXT NOT NULL DEFAULT '',
              thread_type INTEGER,
              first_seen_at INTEGER NOT NULL,
              last_seen_at INTEGER NOT NULL,
              UNIQUE(integration_id, bridge_id, portal_id, portal_receiver)
            );
            CREATE INDEX IF NOT EXISTS external_identities_room
              ON external_identities(matrix_room_id);

            CREATE TABLE IF NOT EXISTS chatwoot_bindings (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              integration_id INTEGER NOT NULL,
              base_url TEXT NOT NULL,
              account_id INTEGER NOT NULL,
              inbox_id INTEGER NOT NULL,
              inbox_identifier TEXT NOT NULL DEFAULT '',
              channel_type TEXT NOT NULL DEFAULT '',
              generation INTEGER NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('PREPARING','ACTIVE','RETIRED','INVALID')),
              reason TEXT NOT NULL DEFAULT '',
              created_at INTEGER NOT NULL,
              activated_at INTEGER NOT NULL DEFAULT 0,
              retired_at INTEGER NOT NULL DEFAULT 0,
              superseded_by INTEGER,
              last_verified_at INTEGER NOT NULL DEFAULT 0,
              last_error TEXT NOT NULL DEFAULT '',
              UNIQUE(integration_id, generation)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_chatwoot_binding
              ON chatwoot_bindings(integration_id) WHERE status='ACTIVE';

            CREATE TABLE IF NOT EXISTS conversation_bindings (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chatwoot_binding_id INTEGER NOT NULL,
              external_identity_id INTEGER NOT NULL,
              matrix_room_id TEXT NOT NULL,
              chatwoot_contact_id INTEGER NOT NULL,
              chatwoot_source_id TEXT NOT NULL,
              chatwoot_conversation_id INTEGER NOT NULL,
              chatwoot_display_id INTEGER,
              status TEXT NOT NULL CHECK(status IN ('ACTIVE','STALE','DELETED','SUPERSEDED','ERROR')),
              last_verified_at INTEGER NOT NULL DEFAULT 0,
              stale_reason TEXT NOT NULL DEFAULT '',
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL,
              UNIQUE(chatwoot_binding_id, external_identity_id),
              UNIQUE(chatwoot_binding_id, chatwoot_conversation_id)
            );
            CREATE INDEX IF NOT EXISTS conversation_bindings_room
              ON conversation_bindings(matrix_room_id, status);
            CREATE INDEX IF NOT EXISTS conversation_bindings_conversation
              ON conversation_bindings(chatwoot_conversation_id, status);

            CREATE TABLE IF NOT EXISTS event_deliveries (
              event_id TEXT NOT NULL,
              chatwoot_binding_id INTEGER NOT NULL,
              direction TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'DELIVERED',
              created_at INTEGER NOT NULL,
              delivered_at INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(event_id, chatwoot_binding_id, direction)
            );
            CREATE INDEX IF NOT EXISTS event_deliveries_seen
              ON event_deliveries(event_id, chatwoot_binding_id);

            CREATE TABLE IF NOT EXISTS legacy_room_link_archive (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chatwoot_binding_id INTEGER,
              room_id TEXT NOT NULL,
              contact_id INTEGER NOT NULL,
              source_id TEXT NOT NULL,
              conversation_id INTEGER NOT NULL,
              created_at INTEGER NOT NULL,
              archived_at INTEGER NOT NULL,
              reason TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reconciliation_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chatwoot_binding_id INTEGER,
              started_at INTEGER NOT NULL,
              completed_at INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL,
              error TEXT NOT NULL DEFAULT ''
            );
            """
        )
        _ensure_column(
            conn, "conversation_bindings", "history_repair_checked_at",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(
            conn, "conversation_bindings", "profile_sync_version",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(
            conn, "conversation_bindings", "profile_synced_at",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(
            conn, "conversation_bindings", "profile_sync_error",
            "TEXT NOT NULL DEFAULT ''",
        )
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_deletions'"
        ).fetchone():
            _ensure_column(conn, "conversation_deletions", "binding_id", "INTEGER")
            _ensure_column(conn, "conversation_deletions", "conversation_binding_id", "INTEGER")
            _ensure_column(conn, "conversation_deletions", "binding_generation", "INTEGER")


def _integration_id(conn):
    now = _now()
    scope = f"matrix:{legacy.MATRIX_ADMIN_MXID}"
    conn.execute(
        "INSERT OR IGNORE INTO integrations(provider,scope_key,created_at,updated_at) "
        "VALUES('facebook',?,?,?)",
        (scope, now, now),
    )
    return int(conn.execute("SELECT id FROM integrations WHERE scope_key=?", (scope,)).fetchone()[0])


def _active_binding():
    with legacy.db() as conn:
        iid = _integration_id(conn)
        return conn.execute(
            "SELECT * FROM chatwoot_bindings WHERE integration_id=? AND status='ACTIVE' LIMIT 1",
            (iid,),
        ).fetchone()


def _binding(binding_id):
    with legacy.db() as conn:
        return conn.execute("SELECT * FROM chatwoot_bindings WHERE id=?", (int(binding_id),)).fetchone()


def _settings_target():
    base = legacy.get_setting("chatwoot_base_url").strip().rstrip("/")
    account = legacy.get_setting("chatwoot_account_id").strip()
    inbox = legacy.get_setting("chatwoot_inbox_id").strip()
    if not base or not account.isdigit() or not inbox.isdigit():
        return None
    return BindingTarget(base, int(account), int(inbox))


def validate_target(base, account, inbox, token):
    base = str(base).strip().rstrip("/")
    account_id = int(account)
    inbox_id = int(inbox)
    response = requests.get(
        f"{base}/api/v1/accounts/{account_id}/inboxes/{inbox_id}",
        headers={"api_access_token": token, "Accept": "application/json"},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json() if response.content else {}
    if not isinstance(data, dict):
        raise RuntimeError("Chatwoot inbox endpoint returned an invalid response")
    channel_type = str(data.get("channel_type") or "")
    if channel_type and channel_type != "Channel::Api":
        raise RuntimeError(f"Configured inbox is {channel_type}, expected Channel::Api")
    channel = data.get("channel") if isinstance(data.get("channel"), dict) else {}
    identifier = str(
        data.get("identifier") or data.get("inbox_identifier") or channel.get("identifier") or ""
    )
    return BindingTarget(base, account_id, inbox_id, identifier, channel_type)


def _archive_room_links(conn, binding_id, reason):
    rows = conn.execute("SELECT * FROM room_links").fetchall()
    now = _now()
    for row in rows:
        conn.execute(
            "INSERT INTO legacy_room_link_archive"
            "(chatwoot_binding_id,room_id,contact_id,source_id,conversation_id,created_at,archived_at,reason) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                binding_id, str(row["room_id"]), int(row["contact_id"]), str(row["source_id"]),
                int(row["conversation_id"]), int(row["created_at"]), now, reason,
            ),
        )
    conn.execute("DELETE FROM room_links")
    return len(rows)


def activate_target(target, reason, force_new=False):
    ensure_schema()
    now = _now()
    with legacy.db() as conn:
        iid = _integration_id(conn)
        active = conn.execute(
            "SELECT * FROM chatwoot_bindings WHERE integration_id=? AND status='ACTIVE' LIMIT 1",
            (iid,),
        ).fetchone()
        if active and not force_new:
            same_key = (
                str(active["base_url"]).rstrip("/"),
                int(active["account_id"]),
                int(active["inbox_id"]),
            ) == target.key
            same_identity = not (
                str(active["inbox_identifier"] or "")
                and target.inbox_identifier
                and str(active["inbox_identifier"]) != target.inbox_identifier
            )
            if same_key and same_identity:
                conn.execute(
                    "UPDATE chatwoot_bindings SET "
                    "inbox_identifier=CASE WHEN ?!='' THEN ? ELSE inbox_identifier END,"
                    "channel_type=CASE WHEN ?!='' THEN ? ELSE channel_type END,"
                    "last_verified_at=?,last_error='' WHERE id=?",
                    (
                        target.inbox_identifier, target.inbox_identifier,
                        target.channel_type, target.channel_type,
                        now, int(active["id"]),
                    ),
                )
                return conn.execute(
                    "SELECT * FROM chatwoot_bindings WHERE id=?", (int(active["id"]),)
                ).fetchone()

        generation = int(conn.execute(
            "SELECT COALESCE(MAX(generation),0)+1 FROM chatwoot_bindings WHERE integration_id=?",
            (iid,),
        ).fetchone()[0])
        cur = conn.execute(
            "INSERT INTO chatwoot_bindings"
            "(integration_id,base_url,account_id,inbox_id,inbox_identifier,channel_type,generation,status,reason,created_at,last_verified_at) "
            "VALUES(?,?,?,?,?,?,?,'PREPARING',?,?,?)",
            (
                iid, target.base_url, target.account_id, target.inbox_id,
                target.inbox_identifier, target.channel_type, generation, reason, now, now,
            ),
        )
        new_id = int(cur.lastrowid)
        archived = 0
        if active:
            old_id = int(active["id"])
            archived = _archive_room_links(conn, old_id, f"binding_superseded:{reason}")
            conn.execute(
                "UPDATE conversation_bindings SET status='SUPERSEDED',stale_reason=?,updated_at=? "
                "WHERE chatwoot_binding_id=? AND status IN ('ACTIVE','STALE','ERROR')",
                (f"binding_superseded:{reason}", now, old_id),
            )
            conn.execute(
                "UPDATE chatwoot_bindings SET status='RETIRED',retired_at=?,superseded_by=?,reason=? "
                "WHERE id=?",
                (now, new_id, reason, old_id),
            )
        conn.execute(
            "UPDATE chatwoot_bindings SET status='ACTIVE',activated_at=?,last_verified_at=? WHERE id=?",
            (now, now, new_id),
        )
        row = conn.execute("SELECT * FROM chatwoot_bindings WHERE id=?", (new_id,)).fetchone()

    legacy.set_setting("chatwoot_binding_id", str(new_id))
    legacy.set_setting("chatwoot_binding_generation", str(generation))
    legacy.set_setting("chatwoot_enabled_at_ms", str(int(time.time() * 1000)))
    print(
        "event=binding_changed "
        f"old_binding_id={int(active['id']) if active else 0} new_binding_id={new_id} "
        f"generation={generation} account_id={target.account_id} inbox_id={target.inbox_id} "
        f"archived_legacy_links={archived} reason={reason}",
        flush=True,
    )
    return row


def ensure_current_binding(validate_remote=False):
    target = _settings_target()
    if target is None:
        return None
    if validate_remote:
        target = validate_target(
            target.base_url, target.account_id, target.inbox_id,
            legacy.get_setting("chatwoot_api_token"),
        )
    active = _active_binding()
    if active:
        same_key = (
            str(active["base_url"]).rstrip("/"),
            int(active["account_id"]),
            int(active["inbox_id"]),
        ) == target.key
        changed_identifier = bool(
            target.inbox_identifier and str(active["inbox_identifier"] or "")
            and target.inbox_identifier != str(active["inbox_identifier"])
        )
        if same_key and not changed_identifier:
            if validate_remote:
                return activate_target(target, "health_verified", force_new=False)
            return active
    return activate_target(target, "startup_or_health_reconcile", force_new=bool(active))


def _meta_identity(room_id):
    try:
        with media._meta_db() as conn:
            portal = conn.execute(
                "SELECT bridge_id,id,receiver,parent_id,metadata FROM portal WHERE mxid=? LIMIT 1",
                (room_id,),
            ).fetchone()
            if not portal:
                raise RuntimeError(f"mautrix-meta has no portal identity for {room_id}")
            users = conn.execute(
                "SELECT id FROM user_login WHERE user_mxid=? ORDER BY id LIMIT 2",
                (legacy.MATRIX_ADMIN_MXID,),
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError(f"cannot read mautrix-meta identity for {room_id}: {exc}") from exc
    metadata = media._json_object(portal[4])
    try:
        thread_type = int(metadata.get("thread_type"))
    except (TypeError, ValueError):
        thread_type = None
    remote_account_id = str(users[0][0]) if len(users) == 1 else str(portal[2] or "")
    return {
        "bridge_id": str(portal[0] or ""),
        "portal_id": str(portal[1] or ""),
        "portal_receiver": str(portal[2] or ""),
        "parent_portal_id": str(portal[3] or ""),
        "thread_type": thread_type,
        "remote_account_id": remote_account_id,
        "matrix_room_id": room_id,
    }


def _external_identity(room_id):
    identity = _meta_identity(room_id)
    now = _now()
    with legacy.db() as conn:
        iid = _integration_id(conn)
        if identity["remote_account_id"]:
            conn.execute(
                "UPDATE integrations SET remote_account_id=?,updated_at=? WHERE id=?",
                (identity["remote_account_id"], now, iid),
            )
        conn.execute(
            "INSERT INTO external_identities"
            "(integration_id,bridge_id,portal_id,portal_receiver,matrix_room_id,parent_portal_id,thread_type,first_seen_at,last_seen_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(integration_id,bridge_id,portal_id,portal_receiver) DO UPDATE SET "
            "matrix_room_id=excluded.matrix_room_id,parent_portal_id=excluded.parent_portal_id,"
            "thread_type=excluded.thread_type,last_seen_at=excluded.last_seen_at",
            (
                iid, identity["bridge_id"], identity["portal_id"], identity["portal_receiver"],
                room_id, identity["parent_portal_id"], identity["thread_type"], now, now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM external_identities WHERE integration_id=? AND bridge_id=? "
            "AND portal_id=? AND portal_receiver=?",
            (iid, identity["bridge_id"], identity["portal_id"], identity["portal_receiver"]),
        ).fetchone()
    return row, identity


def _projection(binding_id, external_id):
    with legacy.db() as conn:
        return conn.execute(
            "SELECT * FROM conversation_bindings WHERE chatwoot_binding_id=? "
            "AND external_identity_id=? LIMIT 1",
            (int(binding_id), int(external_id)),
        ).fetchone()


def _projection_by_conversation(conversation_id, active_only=False):
    with legacy.db() as conn:
        sql = (
            "SELECT cb.*,b.generation,b.status AS binding_status,b.base_url,b.account_id,b.inbox_id "
            "FROM conversation_bindings cb JOIN chatwoot_bindings b ON b.id=cb.chatwoot_binding_id "
            "WHERE cb.chatwoot_conversation_id=? "
        )
        if active_only:
            sql += "AND cb.status='ACTIVE' AND b.status='ACTIVE' "
        sql += "ORDER BY cb.id DESC LIMIT 1"
        return conn.execute(sql, (int(conversation_id),)).fetchone()


def _deleted_intentionally(conversation_id):
    with legacy.db() as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_deletions'"
        ).fetchone():
            return False
        return conn.execute(
            "SELECT 1 FROM conversation_deletions WHERE conversation_id=? LIMIT 1",
            (int(conversation_id),),
        ).fetchone() is not None


def _request(binding, method, path, **kwargs):
    response = requests.request(
        method,
        str(binding["base_url"]).rstrip("/") + path,
        headers={"api_access_token": legacy.get_setting("chatwoot_api_token")},
        timeout=kwargs.pop("timeout", 20),
        **kwargs,
    )
    response.raise_for_status()
    return response.json() if response.content else {}


def _verify_projection(binding, projection, force=False):
    if not force and _now() - int(projection["last_verified_at"] or 0) < VERIFY_TTL_SECONDS:
        return True
    cid = int(projection["chatwoot_conversation_id"])
    try:
        data = _request(
            binding, "GET",
            f"/api/v1/accounts/{int(binding['account_id'])}/conversations/{cid}",
        )
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code != 404:
            raise
        status = "DELETED" if _deleted_intentionally(cid) else "STALE"
        reason = "intentional_delete" if status == "DELETED" else "chatwoot_404"
        with legacy.db() as conn:
            conn.execute(
                "UPDATE conversation_bindings SET status=?,stale_reason=?,updated_at=? WHERE id=?",
                (status, reason, _now(), int(projection["id"])),
            )
        if status == "DELETED":
            raise IntentionalConversationDeletion(f"conversation {cid} has a deletion tombstone")
        print(
            f"event=conversation_mapping_stale binding_id={int(binding['id'])} "
            f"generation={int(binding['generation'])} conversation_id={cid} "
            f"room={projection['matrix_room_id']} reason=chatwoot_404",
            flush=True,
        )
        return False

    actual_inbox = runtime._conversation_inbox_id(data)
    if actual_inbox is None or int(actual_inbox) != int(binding["inbox_id"]):
        with legacy.db() as conn:
            conn.execute(
                "UPDATE conversation_bindings SET status='STALE',stale_reason='conversation_moved_inbox',"
                "updated_at=? WHERE id=?",
                (_now(), int(projection["id"])),
            )
        return False
    with legacy.db() as conn:
        conn.execute(
            "UPDATE conversation_bindings SET status='ACTIVE',stale_reason='',last_verified_at=?,"
            "updated_at=? WHERE id=?",
            (_now(), _now(), int(projection["id"])),
        )
    return True


def _create_lock(binding_id, external_id):
    key = (int(binding_id), int(external_id))
    with _CREATE_LOCKS_GUARD:
        return _CREATE_LOCKS.setdefault(key, threading.RLock())


def _find_contact(binding, account_id, identifier):
    response = requests.get(
        str(binding["base_url"]).rstrip("/") + f"/api/v1/accounts/{account_id}/contacts/search",
        headers={"api_access_token": legacy.get_setting("chatwoot_api_token")},
        params={"q": identifier},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json() if response.content else {}
    for item in (data.get("payload") or []) if isinstance(data, dict) else []:
        if isinstance(item, dict) and str(item.get("identifier") or "") == identifier:
            return item
    return None


def _reconcile_projection_profile(binding, projection, sender, *, force=False):
    """Keep Chatwoot contact presentation in sync without touching message dedupe."""
    if not sender or not enhancements.setting_bool("sync_contact_profiles", True):
        return False
    now = _now()
    current_version = int(projection["profile_sync_version"] or 0)
    synced_at = int(projection["profile_synced_at"] or 0)
    if (
        not force
        and current_version >= PROFILE_SYNC_VERSION
        and synced_at
        and now - synced_at < PROFILE_RECONCILE_TTL_SECONDS
    ):
        return False

    refresh_identity = force or current_version < PROFILE_SYNC_VERSION
    ok = enhancements.update_chatwoot_contact_profile(
        int(binding["account_id"]),
        int(projection["chatwoot_contact_id"]),
        sender,
        force_refresh=refresh_identity,
    )
    with legacy.db() as conn:
        if ok:
            conn.execute(
                "UPDATE conversation_bindings SET profile_sync_version=?,profile_synced_at=?,"
                "profile_sync_error='',updated_at=? WHERE id=?",
                (PROFILE_SYNC_VERSION, now, now, int(projection["id"])),
            )
        else:
            conn.execute(
                "UPDATE conversation_bindings SET profile_sync_error=?,updated_at=? WHERE id=?",
                ("contact_profile_sync_failed", now, int(projection["id"])),
            )
    print(
        f"event=contact_profile_reconciled binding_id={int(binding['id'])} "
        f"conversation_binding_id={int(projection['id'])} room={projection['matrix_room_id']} "
        f"contact_id={int(projection['chatwoot_contact_id'])} success={1 if ok else 0} "
        f"profile_version={PROFILE_SYNC_VERSION}",
        flush=True,
    )
    return bool(ok)


def reconcile_contact_profiles(*, force=False):
    """Reconcile active-generation contact profiles, including pre-existing contacts."""
    binding = _active_binding()
    if not binding or not enhancements.setting_bool("sync_contact_profiles", True):
        return {"checked": 0, "updated": 0, "failed": 0}
    with legacy.db() as conn:
        rows = conn.execute(
            "SELECT cb.* FROM conversation_bindings cb "
            "WHERE cb.chatwoot_binding_id=? AND cb.status='ACTIVE' ORDER BY cb.id",
            (int(binding["id"]),),
        ).fetchall()

    checked = updated = failed = 0
    for projection in rows:
        sender = media._customer_sender_for_room(str(projection["matrix_room_id"]))
        if not sender:
            continue
        checked += 1
        before_version = int(projection["profile_sync_version"] or 0)
        before_synced = int(projection["profile_synced_at"] or 0)
        changed = _reconcile_projection_profile(binding, projection, sender, force=force)
        if changed:
            updated += 1
        else:
            with legacy.db() as conn:
                current = conn.execute(
                    "SELECT profile_sync_version,profile_synced_at,profile_sync_error "
                    "FROM conversation_bindings WHERE id=?",
                    (int(projection["id"]),),
                ).fetchone()
            if current and str(current["profile_sync_error"] or ""):
                failed += 1
            elif (
                before_version < PROFILE_SYNC_VERSION
                or not before_synced
                or force
            ):
                failed += 1
    return {"checked": checked, "updated": updated, "failed": failed}


def _create_projection(binding, external, identity, sender):
    binding_id = int(binding["id"])
    external_id = int(external["id"])
    account_id = int(binding["account_id"])
    inbox_id = int(binding["inbox_id"])
    generation = int(binding["generation"])
    identity_key = f"{identity['bridge_id']}:{identity['portal_id']}:{identity['portal_receiver']}"
    contact_identifier = "meta-contact:" + hashlib.sha256(
        (sender or identity_key).encode()
    ).hexdigest()[:32]
    source_id = "mxg-" + hashlib.sha256(
        f"{binding_id}:{identity_key}".encode()
    ).hexdigest()[:28]
    display_name = str((enhancements.contact_identity(sender) if sender else {}).get("name") or "Meta contact")

    current = _active_binding()
    if not current or int(current["id"]) != binding_id:
        raise BindingChanged("binding changed before projection creation")

    try:
        raw = _request(
            binding, "POST", f"/api/v1/accounts/{account_id}/contacts",
            json={
                "inbox_id": inbox_id,
                "name": display_name,
                "identifier": contact_identifier,
                "additional_attributes": {
                    "matrix_room_id": identity["matrix_room_id"],
                    "meta_bridge_id": identity["bridge_id"],
                    "meta_portal_id": identity["portal_id"],
                    "meta_portal_receiver": identity["portal_receiver"],
                },
            },
        )
        contact = prod.contact_object(raw)
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code not in (409, 422):
            raise
        contact = _find_contact(binding, account_id, contact_identifier)
    if not contact or not contact.get("id"):
        raise RuntimeError("Chatwoot contact could not be created or resolved")

    contact_id = int(contact["id"])
    actual_source = prod.contact_source_id(contact, inbox_id)
    if not actual_source:
        assoc = _request(
            binding, "POST",
            f"/api/v1/accounts/{account_id}/contacts/{contact_id}/contact_inboxes",
            json={"inbox_id": inbox_id, "source_id": source_id},
        )
        actual_source = str((assoc or {}).get("source_id") or source_id)

    current = _active_binding()
    if not current or int(current["id"]) != binding_id:
        raise BindingChanged("binding changed before conversation creation")

    conversation = _request(
        binding, "POST", f"/api/v1/accounts/{account_id}/conversations",
        json={
            "source_id": actual_source,
            "inbox_id": inbox_id,
            "contact_id": contact_id,
            "status": "open",
            "custom_attributes": {
                "bridge_provider": "facebook",
                "meta_bridge_id": identity["bridge_id"],
                "meta_remote_account_id": identity["remote_account_id"],
                "meta_portal_id": identity["portal_id"],
                "meta_portal_receiver": identity["portal_receiver"],
                "matrix_room_id": identity["matrix_room_id"],
                "integration_binding_id": binding_id,
                "binding_generation": generation,
            },
        },
    )
    conversation_id = int(conversation["id"])
    display_id = conversation.get("display_id")
    now = _now()
    current = _active_binding()
    status = "ACTIVE" if current and int(current["id"]) == binding_id else "SUPERSEDED"
    with legacy.db() as conn:
        conn.execute(
            "INSERT INTO conversation_bindings"
            "(chatwoot_binding_id,external_identity_id,matrix_room_id,chatwoot_contact_id,"
            "chatwoot_source_id,chatwoot_conversation_id,chatwoot_display_id,status,"
            "last_verified_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(chatwoot_binding_id,external_identity_id) DO UPDATE SET "
            "matrix_room_id=excluded.matrix_room_id,chatwoot_contact_id=excluded.chatwoot_contact_id,"
            "chatwoot_source_id=excluded.chatwoot_source_id,"
            "chatwoot_conversation_id=excluded.chatwoot_conversation_id,"
            "chatwoot_display_id=excluded.chatwoot_display_id,status=excluded.status,"
            "stale_reason='',last_verified_at=excluded.last_verified_at,updated_at=excluded.updated_at",
            (
                binding_id, external_id, identity["matrix_room_id"], contact_id,
                actual_source, conversation_id,
                int(display_id) if str(display_id or "").isdigit() else None,
                status, now, now, now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM conversation_bindings WHERE chatwoot_binding_id=? "
            "AND external_identity_id=?",
            (binding_id, external_id),
        ).fetchone()
        if status == "ACTIVE":
            conn.execute(
                "INSERT OR REPLACE INTO room_links"
                "(room_id,contact_id,source_id,conversation_id,created_at) VALUES(?,?,?,?,?)",
                (
                    identity["matrix_room_id"], contact_id, actual_source,
                    conversation_id, now,
                ),
            )
    if status != "ACTIVE":
        raise BindingChanged("binding changed after conversation creation")
    _clear_binding_dedupe_for_room(
        binding_id, identity["matrix_room_id"], "fresh_conversation_projection"
    )
    _reconcile_projection_profile(binding, row, sender, force=True)
    print(
        f"event=conversation_projection_created binding_id={binding_id} generation={generation} "
        f"room={identity['matrix_room_id']} portal_id={identity['portal_id']} "
        f"conversation_id={conversation_id}",
        flush=True,
    )
    return row


def _adopt_legacy(binding, external, identity):
    with legacy.db() as conn:
        link = conn.execute(
            "SELECT * FROM room_links WHERE room_id=?", (identity["matrix_room_id"],)
        ).fetchone()
    if not link:
        return None
    try:
        data = _request(
            binding, "GET",
            f"/api/v1/accounts/{int(binding['account_id'])}/conversations/{int(link['conversation_id'])}",
        )
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise
    actual_inbox = runtime._conversation_inbox_id(data)
    if actual_inbox is None or int(actual_inbox) != int(binding["inbox_id"]):
        return None
    now = _now()
    with legacy.db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO conversation_bindings"
            "(chatwoot_binding_id,external_identity_id,matrix_room_id,chatwoot_contact_id,"
            "chatwoot_source_id,chatwoot_conversation_id,chatwoot_display_id,status,"
            "last_verified_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'ACTIVE',?,?,?)",
            (
                int(binding["id"]), int(external["id"]), identity["matrix_room_id"],
                int(link["contact_id"]), str(link["source_id"]), int(link["conversation_id"]),
                int(data.get("display_id")) if str(data.get("display_id") or "").isdigit() else None,
                now, int(link["created_at"]), now,
            ),
        )
        adopted = conn.execute(
            "SELECT * FROM conversation_bindings WHERE chatwoot_binding_id=? "
            "AND external_identity_id=?",
            (int(binding["id"]), int(external["id"])),
        ).fetchone()
    migrated = _migrate_legacy_dedupe_for_room(
        int(binding["id"]), identity["matrix_room_id"]
    )
    if migrated:
        print(
            f"event=legacy_room_dedupe_migrated binding_id={int(binding['id'])} "
            f"room={identity['matrix_room_id']} events={migrated}",
            flush=True,
        )
    return adopted


def ensure_room_link(room_id, sender, force_verify=False):
    ensure_schema()
    for attempt in range(2):
        binding = ensure_current_binding(validate_remote=False)
        if binding is None:
            raise RuntimeError("Chatwoot is not configured")
        external, identity = _external_identity(room_id)
        binding_id = int(binding["id"])
        external_id = int(external["id"])
        with _create_lock(binding_id, external_id):
            projection = _projection(binding_id, external_id)
            if projection and str(projection["status"]) == "ACTIVE":
                if _verify_projection(binding, projection, force=force_verify):
                    _reconcile_projection_profile(binding, projection, sender)
                    with legacy.db() as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO room_links"
                            "(room_id,contact_id,source_id,conversation_id,created_at) VALUES(?,?,?,?,?)",
                            (
                                room_id, int(projection["chatwoot_contact_id"]),
                                str(projection["chatwoot_source_id"]),
                                int(projection["chatwoot_conversation_id"]),
                                int(projection["created_at"]),
                            ),
                        )
                        return conn.execute(
                            "SELECT * FROM room_links WHERE room_id=?", (room_id,)
                        ).fetchone()
            if projection and str(projection["status"]) == "DELETED":
                raise IntentionalConversationDeletion(
                    f"room {room_id} belongs to an intentionally deleted conversation"
                )
            if projection is None:
                adopted = _adopt_legacy(binding, external, identity)
                if adopted:
                    _reconcile_projection_profile(binding, adopted, sender, force=True)
                    with legacy.db() as conn:
                        return conn.execute(
                            "SELECT * FROM room_links WHERE room_id=?", (room_id,)
                        ).fetchone()
            try:
                with _LOCK:
                    current = _active_binding()
                    if not current or int(current["id"]) != binding_id:
                        raise BindingChanged("binding changed before create")
                    _create_projection(binding, external, identity, sender)
            except BindingChanged:
                if attempt == 0:
                    continue
                raise
            with legacy.db() as conn:
                return conn.execute(
                    "SELECT * FROM room_links WHERE room_id=?", (room_id,)
                ).fetchone()
    raise RuntimeError("could not resolve the current Chatwoot conversation binding")


def _active_conversation_id(room_id):
    with legacy.db() as conn:
        row = conn.execute(
            "SELECT cb.chatwoot_conversation_id FROM conversation_bindings cb "
            "JOIN chatwoot_bindings b ON b.id=cb.chatwoot_binding_id "
            "WHERE cb.matrix_room_id=? AND cb.status='ACTIVE' AND b.status='ACTIVE' "
            "ORDER BY cb.id DESC LIMIT 1",
            (room_id,),
        ).fetchone()
    return int(row[0]) if row else None


def room_matches_configured_chatwoot_inbox(room_id):
    cid = _active_conversation_id(room_id)
    if cid is None:
        raise RuntimeError("Matrix room has no active-generation Chatwoot conversation")
    projection = _projection_by_conversation(cid, active_only=True)
    if not projection:
        raise RuntimeError("conversation is not part of the active Chatwoot generation")
    binding = _binding(int(projection["chatwoot_binding_id"]))
    valid = _verify_projection(binding, projection, force=False)
    return valid, cid, int(binding["inbox_id"])


def repair_deleted_conversation(room_id):
    cid = _active_conversation_id(room_id)
    if cid is None:
        return False
    sender = media._customer_sender_for_room(room_id)
    if not sender:
        return False
    try:
        link = ensure_room_link(room_id, sender, force_verify=True)
    except IntentionalConversationDeletion:
        return False
    return int(link["conversation_id"]) != cid


def _recover_404(conversation_id):
    projection = _projection_by_conversation(conversation_id, active_only=True)
    if not projection:
        raise BindingChanged(
            f"conversation {conversation_id} is not in the active binding generation"
        )
    if _deleted_intentionally(conversation_id):
        with legacy.db() as conn:
            conn.execute(
                "UPDATE conversation_bindings SET status='DELETED',"
                "stale_reason='intentional_delete',updated_at=? WHERE id=?",
                (_now(), int(projection["id"])),
            )
        raise IntentionalConversationDeletion(
            f"conversation {conversation_id} was intentionally deleted"
        )
    with legacy.db() as conn:
        conn.execute(
            "UPDATE conversation_bindings SET status='STALE',stale_reason='runtime_404',"
            "updated_at=? WHERE id=?",
            (_now(), int(projection["id"])),
        )
        conn.execute(
            "DELETE FROM room_links WHERE room_id=? AND conversation_id=?",
            (str(projection["matrix_room_id"]), int(conversation_id)),
        )
    sender = media._customer_sender_for_room(str(projection["matrix_room_id"]))
    link = ensure_room_link(str(projection["matrix_room_id"]), sender)
    print(
        f"event=conversation_rebound old_conversation_id={conversation_id} "
        f"new_conversation_id={int(link['conversation_id'])} "
        f"room={projection['matrix_room_id']} retry=1",
        flush=True,
    )
    return link


def _message_items(payload):
    """Normalize Chatwoot conversation-message response shapes."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    value = payload.get("payload")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        nested = value.get("messages")
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
    value = payload.get("messages")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _matrix_event_id_from_message(message):
    attributes = message.get("content_attributes") if isinstance(message, dict) else {}
    if isinstance(attributes, str):
        try:
            import json
            attributes = json.loads(attributes)
        except (TypeError, ValueError):
            attributes = {}
    if not isinstance(attributes, dict):
        return ""
    return str(attributes.get("matrix_event_id") or "")


def _remote_event_delivery(conversation_id, event_id):
    """Return the existing Chatwoot message for a Matrix event, if present.

    This closes the crash/timeout window between Chatwoot committing a POST and the
    integration durably recording event_deliveries. A retried Matrix event first
    checks Chatwoot's durable matrix_event_id marker and reuses the existing row.
    """
    if not event_id:
        return None
    projection = _projection_by_conversation(conversation_id, active_only=True)
    if not projection:
        raise BindingChanged(
            f"conversation {conversation_id} is not in the active binding generation"
        )
    binding = _binding(int(projection["chatwoot_binding_id"]))
    payload = _request(
        binding,
        "GET",
        f"/api/v1/accounts/{int(binding['account_id'])}/conversations/{int(conversation_id)}/messages",
    )
    for message in _message_items(payload):
        if _matrix_event_id_from_message(message) == str(event_id):
            return message
    return None


def _post_idempotent(conversation_id, base_post, kwargs):
    event_id = str(kwargs.get("event_id") or "")
    try:
        existing = _remote_event_delivery(conversation_id, event_id) if event_id else None
        if existing is not None:
            print(
                f"event=matrix_delivery_remote_dedupe conversation_id={int(conversation_id)} "
                f"matrix_event_id={event_id}",
                flush=True,
            )
            return existing
        return base_post(conversation_id, **kwargs)
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code != 404:
            raise
        link = _recover_404(conversation_id)
        rebound_id = int(link["conversation_id"])
        existing = _remote_event_delivery(rebound_id, event_id) if event_id else None
        if existing is not None:
            print(
                f"event=matrix_delivery_remote_dedupe conversation_id={rebound_id} "
                f"matrix_event_id={event_id} after_rebind=1",
                flush=True,
            )
            return existing
        return base_post(rebound_id, **kwargs)


def post_text(conversation_id, **kwargs):
    with _LOCK:
        if not _projection_by_conversation(conversation_id, active_only=True):
            raise BindingChanged("refusing text send through an old binding generation")
        return _post_idempotent(conversation_id, _base_post_text, kwargs)


def post_media(conversation_id, **kwargs):
    with _LOCK:
        if not _projection_by_conversation(conversation_id, active_only=True):
            raise BindingChanged("refusing media send through an old binding generation")
        return _post_idempotent(conversation_id, _base_post_media, kwargs)


def sync_conversation_context(room_id, link):
    with _LOCK:
        current = _active_conversation_id(room_id)
        if current is None or int(link["conversation_id"]) != current:
            link = ensure_room_link(room_id, media._customer_sender_for_room(room_id))
        return _base_context_sync(room_id, link)


def event_seen(event_id):
    if not event_id:
        return False
    binding = _active_binding()
    if not binding:
        return _base_event_seen(event_id)
    with legacy.db() as conn:
        return conn.execute(
            "SELECT 1 FROM event_deliveries WHERE event_id=? AND chatwoot_binding_id=? LIMIT 1",
            (event_id, int(binding["id"])),
        ).fetchone() is not None


def mark_event(event_id, direction):
    if not event_id:
        return
    binding = _active_binding()
    if not binding:
        return _base_mark_event(event_id, direction)
    now = _now()
    with legacy.db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO event_deliveries"
            "(event_id,chatwoot_binding_id,direction,status,created_at,delivered_at) "
            "VALUES(?,?,?,'DELIVERED',?,?)",
            (event_id, int(binding["id"]), direction, now, now),
        )
    _base_mark_event(event_id, direction)


def _room_event_ids(room_id):
    return rebuild._room_meta_event_ids(room_id)


def _migrate_legacy_dedupe_for_room(binding_id, room_id):
    """Copy legacy dedupe only for a room whose old Chatwoot conversation was adopted."""
    event_ids = _room_event_ids(room_id)
    if not event_ids:
        return 0
    now = _now()
    migrated = 0
    with legacy.db() as conn:
        for event_id in event_ids:
            row = conn.execute(
                "SELECT direction,created_at FROM processed_events WHERE event_id=? LIMIT 1",
                (event_id,),
            ).fetchone()
            if not row:
                continue
            cursor = conn.execute(
                "INSERT OR IGNORE INTO event_deliveries"
                "(event_id,chatwoot_binding_id,direction,status,created_at,delivered_at) "
                "VALUES(?,?,?,'DELIVERED',?,?)",
                (
                    event_id, int(binding_id), str(row["direction"]),
                    int(row["created_at"] or now), int(row["created_at"] or now),
                ),
            )
            migrated += max(0, int(cursor.rowcount or 0))
    return migrated


def _clear_binding_dedupe_for_room(binding_id, room_id, reason):
    event_ids = _room_event_ids(room_id)
    if not event_ids:
        return 0
    removed = 0
    with legacy.db() as conn:
        for event_id in event_ids:
            cursor = conn.execute(
                "DELETE FROM event_deliveries WHERE event_id=? AND chatwoot_binding_id=?",
                (event_id, int(binding_id)),
            )
            removed += max(0, int(cursor.rowcount or 0))
    if removed:
        print(
            f"event=binding_room_dedupe_reset binding_id={int(binding_id)} "
            f"room={room_id} events={removed} reason={reason}",
            flush=True,
        )
    return removed


def _substantive_chatwoot_messages(payload):
    if not isinstance(payload, dict):
        return []
    messages = payload.get("payload")
    if isinstance(messages, dict):
        messages = messages.get("messages")
    if not isinstance(messages, list):
        messages = payload.get("messages")
    if not isinstance(messages, list):
        return []
    substantive = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        message_type = item.get("message_type")
        if message_type in (0, 1, "incoming", "outgoing") and item.get("private") is not True:
            substantive.append(item)
    return substantive


def _repair_empty_projection_dedupe(room_id):
    """Repair the v12 rollout bug that inherited old global dedupe into fresh projections."""
    cid = _active_conversation_id(room_id)
    if cid is None:
        return 0
    projection = _projection_by_conversation(cid, active_only=True)
    if not projection or int(projection["history_repair_checked_at"] or 0):
        return 0
    binding = _binding(int(projection["chatwoot_binding_id"]))
    if not binding:
        return 0

    data = _request(
        binding,
        "GET",
        f"/api/v1/accounts/{int(binding['account_id'])}/conversations/{cid}/messages",
    )
    substantive = _substantive_chatwoot_messages(data)
    removed = 0
    if not substantive:
        removed = _clear_binding_dedupe_for_room(
            int(binding["id"]), room_id, "empty_projection_history_repair"
        )
    with legacy.db() as conn:
        conn.execute(
            "UPDATE conversation_bindings SET history_repair_checked_at=?,updated_at=? WHERE id=?",
            (_now(), _now(), int(projection["id"])),
        )
    return removed


def _seed_dedupe():
    """Do not globally inherit pre-generation processed_events into a new binding.

    Legacy dedupe is migrated per room only when that exact legacy conversation is
    adopted by _adopt_legacy(). A fresh projection must be eligible for history import.
    """
    return 0


def import_recent_history(room_id):
    _repair_empty_projection_dedupe(room_id)
    return _base_import_history(room_id)


def mirror_matrix_event(room_id, event, history=False):
    try:
        if event.get("type") == "m.room.message":
            sender = str(event.get("sender") or "")
            if sender != legacy.bridge_bot_mxid():
                customer = (
                    sender if media.message_direction(event) == "incoming"
                    else media._customer_sender_for_room(room_id)
                )
                if customer:
                    ensure_room_link(room_id, customer)
        return _base_mirror(room_id, event, history=history)
    except IntentionalConversationDeletion:
        event_id = str(event.get("event_id") or "")
        if event_id:
            mark_event(event_id, "suppressed_intentional_chatwoot_delete")
        print(
            f"event=matrix_delivery_suppressed room={room_id} "
            "reason=intentional_chatwoot_delete",
            flush=True,
        )
        return False


def reset_target_state(old_target, new_target):
    if old_target == new_target or not any(new_target):
        return
    validated = _VALIDATED_TARGET.get()
    if validated and tuple(validated["key"]) == tuple(new_target):
        target = validated["target"]
    else:
        target = BindingTarget(
            str(new_target[0]).rstrip("/"), int(new_target[1]), int(new_target[2])
        )
    activate_target(target, "configuration_change", force_new=True)

    for key in (
        "chatwoot_api_inbox_signing_secret",
        "chatwoot_webhook_signing_secret",
        "api_inbox_callback_verified_at",
        "api_inbox_delivery_verified_at",
        "webhook_registration_verified_at",
        "webhook_delivery_verified_at",
        "webhook_last_delivery_id",
        "last_chatwoot_matrix_delivery_at",
        "last_chatwoot_matrix_event_id",
        "last_chatwoot_matrix_conversation_id",
        "last_chatwoot_matrix_error",
        "last_chatwoot_matrix_error_at",
        "historical_direction_repair_needed",
    ):
        legacy.set_setting(key, "")


def save_configuration(base, account, inbox, token, proxy_enabled, proxy_url,
                       webhook_signing_secret=""):
    effective_token = (token or "").strip() or legacy.get_setting("chatwoot_api_token")
    if not effective_token:
        raise ValueError("Chatwoot API token is required")
    target = validate_target(base, account, inbox, effective_token)
    marker = _VALIDATED_TARGET.set(
        {
            "key": (target.base_url, str(target.account_id), str(target.inbox_id)),
            "target": target,
        }
    )
    try:
        return _base_save_configuration(
            base, account, inbox, token, proxy_enabled, proxy_url,
            webhook_signing_secret=webhook_signing_secret,
        )
    finally:
        _VALIDATED_TARGET.reset(marker)


def lifecycle_start(conversation_id, room_id, origin, state):
    _base_lifecycle_start(conversation_id, room_id, origin, state)
    projection = _projection_by_conversation(conversation_id, active_only=False)
    if not projection:
        return
    with legacy.db() as conn:
        conn.execute(
            "UPDATE conversation_deletions SET binding_id=?,conversation_binding_id=?,"
            "binding_generation=? WHERE conversation_id=? AND binding_id IS NULL",
            (
                int(projection["chatwoot_binding_id"]), int(projection["id"]),
                int(projection["generation"]), int(conversation_id),
            ),
        )


def lifecycle_delete_chatwoot(conversation_id):
    with legacy.db() as conn:
        op = conn.execute(
            "SELECT binding_id FROM conversation_deletions WHERE conversation_id=?",
            (int(conversation_id),),
        ).fetchone()
    binding_id = int(op[0]) if op and op[0] is not None else 0
    if not binding_id:
        projection = _projection_by_conversation(conversation_id, active_only=False)
        binding_id = int(projection["chatwoot_binding_id"]) if projection else 0
    binding = _binding(binding_id) if binding_id else None
    if not binding:
        return _base_lifecycle_delete(conversation_id)

    response = requests.delete(
        str(binding["base_url"]).rstrip("/")
        + f"/api/v1/accounts/{int(binding['account_id'])}/conversations/{int(conversation_id)}",
        headers={"api_access_token": legacy.get_setting("chatwoot_api_token")},
        timeout=20,
    )
    if response.status_code != 404:
        response.raise_for_status()


def reconcile_binding_health():
    binding = _active_binding()
    with legacy.db() as conn:
        run_id = int(conn.execute(
            "INSERT INTO reconciliation_runs(chatwoot_binding_id,started_at,status) "
            "VALUES(?,?,'RUNNING')",
            (int(binding["id"]) if binding else None, _now()),
        ).lastrowid)
    try:
        target = _settings_target()
        if target is None:
            result = {"ok": True, "configured": False}
        else:
            verified = validate_target(
                target.base_url, target.account_id, target.inbox_id,
                legacy.get_setting("chatwoot_api_token"),
            )
            active = _active_binding()
            force_new = bool(
                active
                and str(active["inbox_identifier"] or "")
                and verified.inbox_identifier
                and str(active["inbox_identifier"]) != verified.inbox_identifier
            )
            active = activate_target(
                verified,
                "inbox_identity_changed" if force_new else "health_verified",
                force_new=force_new,
            )
            legacy.set_setting("chatwoot_binding_health_error", "")
            result = {
                "ok": True,
                "binding_id": int(active["id"]),
                "generation": int(active["generation"]),
            }
        with legacy.db() as conn:
            conn.execute(
                "UPDATE reconciliation_runs SET completed_at=?,status='COMPLETED' WHERE id=?",
                (_now(), run_id),
            )
        return result
    except Exception as exc:
        legacy.set_setting(
            "chatwoot_binding_health_error", f"{type(exc).__name__}: {exc}"[:1000]
        )
        with legacy.db() as conn:
            conn.execute(
                "UPDATE reconciliation_runs SET completed_at=?,status='FAILED',error=? WHERE id=?",
                (_now(), str(exc)[:1000], run_id),
            )
        return {"ok": False, "error": str(exc)}


def reconcile_meta_portals():
    result = _base_reconcile()
    health = reconcile_binding_health()
    profiles = reconcile_contact_profiles()
    successful = bool(health.get("ok")) and not (
        isinstance(result, dict) and (result.get("errors") or [])
    )
    if successful:
        legacy.set_setting("runtime_reconcile_version", RUNTIME_RECONCILE_VERSION)
        legacy.set_setting("runtime_reconcile_pending", "0")
        legacy.set_setting("runtime_reconcile_completed_at", str(_now()))
    if isinstance(result, dict):
        result["chatwoot_binding"] = health
        result["contact_profiles"] = profiles
        result["runtime_reconcile_version"] = RUNTIME_RECONCILE_VERSION
        result["runtime_reconcile_pending"] = not successful
    return result


def install():
    global _INSTALLED, _base_save_configuration, _base_post_text, _base_post_media
    global _base_context_sync, _base_mirror, _base_reconcile, _base_import_history
    global _base_lifecycle_start, _base_lifecycle_delete
    if _INSTALLED:
        return

    ensure_schema()
    previous_reconcile_version = legacy.get_setting("runtime_reconcile_version")
    if previous_reconcile_version != RUNTIME_RECONCILE_VERSION:
        legacy.set_setting("runtime_reconcile_pending", "1")
        legacy.set_setting(
            "runtime_reconcile_reason",
            f"runtime_upgrade:{previous_reconcile_version or 'none'}->{RUNTIME_RECONCILE_VERSION}",
        )
        print(
            f"event=runtime_reconcile_requested reason=runtime_upgrade "
            f"from_version={previous_reconcile_version or 'none'} "
            f"to_version={RUNTIME_RECONCILE_VERSION}",
            flush=True,
        )
    try:
        ensure_current_binding(validate_remote=False)
    except Exception as exc:
        print(
            f"binding generation bootstrap deferred: {type(exc).__name__}: {exc}",
            flush=True,
        )
    _seed_dedupe()

    _base_save_configuration = nicegui_app._legacy_ui.save_configuration
    _base_post_text = media._post_chatwoot_text
    _base_post_media = media.post_chatwoot_media
    _base_context_sync = media.sync_conversation_context
    _base_mirror = media.mirror_matrix_event
    _base_reconcile = portal_reconcile.reconcile_meta_portals
    _base_import_history = enhancements.import_recent_history
    _base_lifecycle_start = lifecycle._start_operation
    _base_lifecycle_delete = lifecycle._delete_chatwoot_conversation

    prod.ensure_room_link = ensure_room_link
    legacy.ensure_room_link = ensure_room_link
    legacy.event_seen = event_seen
    legacy.mark_event = mark_event

    runtime._linked_conversation_id = _active_conversation_id
    runtime.room_matches_configured_chatwoot_inbox = room_matches_configured_chatwoot_inbox
    runtime._reset_chatwoot_target_state = reset_target_state

    enhancements.repair_deleted_conversation = repair_deleted_conversation
    enhancements.import_recent_history = import_recent_history
    rebuild.repair_deleted_conversation = repair_deleted_conversation
    media._post_chatwoot_text = post_text
    media.post_chatwoot_media = post_media
    media.sync_conversation_context = sync_conversation_context
    media.mirror_matrix_event = mirror_matrix_event

    nicegui_app._legacy_ui.save_configuration = save_configuration
    portal_reconcile.reconcile_meta_portals = reconcile_meta_portals

    lifecycle._start_operation = lifecycle_start
    lifecycle._delete_chatwoot_conversation = lifecycle_delete_chatwoot

    _INSTALLED = True
