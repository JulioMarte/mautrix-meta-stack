import type { Database } from "bun:sqlite";

export const LATEST_SCHEMA_VERSION = 5;

const migration1 = `
CREATE TABLE IF NOT EXISTS tenants (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('active','disabled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS egress_profiles (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  scheme TEXT NOT NULL,
  host TEXT NOT NULL,
  port INTEGER NOT NULL CHECK(port > 0 AND port <= 65535),
  username TEXT,
  secret_ref TEXT,
  country TEXT,
  region TEXT,
  sticky_session_id TEXT,
  expected_exit_ip TEXT,
  last_verified_exit_ip TEXT,
  status TEXT NOT NULL,
  last_checked_at TEXT,
  failure_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chatwoot_bindings (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  chatwoot_account_id TEXT NOT NULL,
  chatwoot_inbox_id TEXT NOT NULL,
  api_base_url TEXT NOT NULL,
  credential_ref TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, chatwoot_account_id, chatwoot_inbox_id)
);
CREATE TABLE IF NOT EXISTS meta_connections (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  provider TEXT NOT NULL CHECK(provider = 'facebook'),
  meta_account_id TEXT,
  mautrix_login_id TEXT,
  matrix_owner_mxid TEXT NOT NULL,
  chatwoot_binding_id TEXT REFERENCES chatwoot_bindings(id),
  egress_profile_id TEXT REFERENCES egress_profiles(id),
  egress_policy TEXT NOT NULL CHECK(egress_policy IN ('direct_allowed','proxy_preferred','proxy_required')),
  status TEXT NOT NULL CHECK(status IN ('draft','ready','active','degraded','blocked','disabled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(provider, meta_account_id),
  UNIQUE(mautrix_login_id)
);
CREATE TABLE IF NOT EXISTS conversation_bindings (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  meta_connection_id TEXT NOT NULL REFERENCES meta_connections(id),
  matrix_room_id TEXT NOT NULL,
  remote_thread_id TEXT NOT NULL,
  remote_contact_id TEXT,
  chatwoot_account_id TEXT NOT NULL,
  chatwoot_inbox_id TEXT NOT NULL,
  chatwoot_contact_id TEXT,
  chatwoot_source_id TEXT,
  chatwoot_conversation_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(meta_connection_id, remote_thread_id),
  UNIQUE(tenant_id, chatwoot_account_id, chatwoot_inbox_id, chatwoot_conversation_id)
);
CREATE TABLE IF NOT EXISTS processed_events (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  source_event_id TEXT NOT NULL,
  meta_connection_id TEXT REFERENCES meta_connections(id),
  payload_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  processed_at TEXT,
  last_error TEXT,
  UNIQUE(source, source_event_id)
);
CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT PRIMARY KEY,
  tenant_id TEXT REFERENCES tenants(id),
  actor_type TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  action TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  before_json TEXT,
  after_json TEXT,
  created_at TEXT NOT NULL
);
`;

const migration2 = `
CREATE TABLE IF NOT EXISTS chatwoot_webhook_configs (
  binding_id TEXT PRIMARY KEY REFERENCES chatwoot_bindings(id) ON DELETE CASCADE,
  secret_ref TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
`;

const migration3 = `
CREATE TABLE IF NOT EXISTS provisioning_claims (
  id TEXT PRIMARY KEY,
  secret_digest TEXT NOT NULL UNIQUE,
  meta_connection_id TEXT NOT NULL REFERENCES meta_connections(id) ON DELETE CASCADE,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  matrix_owner_mxid TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT,
  revoked_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provisioning_claims_connection
  ON provisioning_claims(meta_connection_id, created_at);
CREATE INDEX IF NOT EXISTS idx_provisioning_claims_expiry
  ON provisioning_claims(expires_at) WHERE used_at IS NULL AND revoked_at IS NULL;
`;

const migration4 = `
CREATE TABLE IF NOT EXISTS matrix_room_bindings (
  matrix_room_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  meta_connection_id TEXT NOT NULL REFERENCES meta_connections(id),
  remote_thread_id TEXT NOT NULL,
  mautrix_login_id TEXT NOT NULL,
  bridge_state_key TEXT NOT NULL,
  source_event_id TEXT,
  verified_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(meta_connection_id, remote_thread_id)
);
CREATE INDEX IF NOT EXISTS idx_matrix_room_bindings_connection
  ON matrix_room_bindings(meta_connection_id, matrix_room_id);
CREATE TABLE IF NOT EXISTS matrix_sync_checkpoints (
  consumer_id TEXT PRIMARY KEY,
  next_batch TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
`;

const migration5 = `
CREATE UNIQUE INDEX IF NOT EXISTS idx_meta_connections_live_egress_unique
  ON meta_connections(egress_profile_id)
  WHERE egress_profile_id IS NOT NULL AND status <> 'disabled';
`;

export function runMigrations(db: Database): void {
  db.exec("PRAGMA foreign_keys = ON; PRAGMA journal_mode = WAL;");
  db.exec("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)");
  const applied = db.query("SELECT version FROM schema_migrations ORDER BY version").all() as Array<{ version: number }>;
  const versions = new Set(applied.map((row) => row.version));
  if (!versions.has(1)) {
    const apply = db.transaction(() => {
      db.exec(migration1);
      db.query("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)").run(1, new Date().toISOString());
    });
    apply();
  }
  if (!versions.has(2)) {
    const apply = db.transaction(() => {
      db.exec(migration2);
      db.query("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)").run(2, new Date().toISOString());
    });
    apply();
  }
  if (!versions.has(3)) {
    const apply = db.transaction(() => {
      db.exec(migration3);
      db.query("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)").run(3, new Date().toISOString());
    });
    apply();
  }
  if (!versions.has(4)) {
    const apply = db.transaction(() => {
      db.exec(migration4);
      db.query("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)").run(4, new Date().toISOString());
    });
    apply();
  }
  if (!versions.has(5)) {
    const apply = db.transaction(() => {
      db.exec(migration5);
      db.query("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)").run(5, new Date().toISOString());
    });
    apply();
  }
}

export function schemaVersion(db: Database): number {
  const row = db.query("SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations").get() as { version: number } | null;
  return row?.version ?? 0;
}
