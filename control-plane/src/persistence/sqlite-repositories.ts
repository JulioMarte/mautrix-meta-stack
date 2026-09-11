import type { Database } from "bun:sqlite";
import type { AuditEvent, AuditRepository, ConnectionStatus, EgressPolicy, EgressProfile, EgressProfileRepository, EgressStatus, MetaConnection, MetaConnectionRepository, Tenant, TenantRepository } from "../domain/models";

function tenantFromRow(row: Record<string, unknown>): Tenant {
  return { id: String(row.id), slug: String(row.slug), name: String(row.name), status: row.status as Tenant["status"], createdAt: String(row.created_at), updatedAt: String(row.updated_at) };
}

function connectionFromRow(row: Record<string, unknown>): MetaConnection {
  return {
    id: String(row.id), tenantId: String(row.tenant_id), provider: "facebook",
    metaAccountId: row.meta_account_id == null ? null : String(row.meta_account_id),
    mautrixLoginId: row.mautrix_login_id == null ? null : String(row.mautrix_login_id),
    matrixOwnerMxid: String(row.matrix_owner_mxid), chatwootBindingId: row.chatwoot_binding_id == null ? null : String(row.chatwoot_binding_id),
    egressProfileId: row.egress_profile_id == null ? null : String(row.egress_profile_id), egressPolicy: row.egress_policy as EgressPolicy,
    status: row.status as ConnectionStatus, createdAt: String(row.created_at), updatedAt: String(row.updated_at)
  };
}

function egressFromRow(row: Record<string, unknown>): EgressProfile {
  return {
    id: String(row.id), provider: String(row.provider), scheme: String(row.scheme), host: String(row.host), port: Number(row.port),
    username: row.username == null ? null : String(row.username), secretRef: row.secret_ref == null ? null : String(row.secret_ref),
    country: row.country == null ? null : String(row.country), region: row.region == null ? null : String(row.region),
    stickySessionId: row.sticky_session_id == null ? null : String(row.sticky_session_id), expectedExitIp: row.expected_exit_ip == null ? null : String(row.expected_exit_ip),
    lastVerifiedExitIp: row.last_verified_exit_ip == null ? null : String(row.last_verified_exit_ip), status: row.status as EgressStatus,
    lastCheckedAt: row.last_checked_at == null ? null : String(row.last_checked_at), failureCount: Number(row.failure_count),
    createdAt: String(row.created_at), updatedAt: String(row.updated_at)
  };
}

function auditFromRow(row: Record<string, unknown>): AuditEvent {
  return {
    id: String(row.id), tenantId: row.tenant_id == null ? null : String(row.tenant_id), actorType: String(row.actor_type), actorId: String(row.actor_id),
    action: String(row.action), entityType: String(row.entity_type), entityId: String(row.entity_id), beforeJson: row.before_json == null ? null : String(row.before_json),
    afterJson: row.after_json == null ? null : String(row.after_json), createdAt: String(row.created_at)
  };
}

export class SQLiteTenantRepository implements TenantRepository {
  constructor(private readonly db: Database) {}
  create(input: { slug: string; name: string }): Tenant {
    const now = new Date().toISOString();
    const tenant: Tenant = { id: crypto.randomUUID(), slug: input.slug, name: input.name, status: "active", createdAt: now, updatedAt: now };
    this.db.query("INSERT INTO tenants(id, slug, name, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)").run(tenant.id, tenant.slug, tenant.name, tenant.status, tenant.createdAt, tenant.updatedAt);
    return tenant;
  }
  findById(id: string): Tenant | null {
    const row = this.db.query("SELECT * FROM tenants WHERE id = ?").get(id) as Record<string, unknown> | null;
    return row ? tenantFromRow(row) : null;
  }
  list(): Tenant[] { return (this.db.query("SELECT * FROM tenants ORDER BY created_at, id").all() as Array<Record<string, unknown>>).map(tenantFromRow); }
}

export class SQLiteMetaConnectionRepository implements MetaConnectionRepository {
  constructor(private readonly db: Database) {}
  create(input: { tenantId: string; matrixOwnerMxid: string; egressPolicy?: EgressPolicy; metaAccountId?: string; mautrixLoginId?: string }): MetaConnection {
    if (!this.db.query("SELECT id FROM tenants WHERE id = ? AND status = 'active'").get(input.tenantId)) throw new Error("TENANT_NOT_ACTIVE");
    const now = new Date().toISOString();
    const connection: MetaConnection = {
      id: crypto.randomUUID(), tenantId: input.tenantId, provider: "facebook", metaAccountId: input.metaAccountId ?? null, mautrixLoginId: input.mautrixLoginId ?? null,
      matrixOwnerMxid: input.matrixOwnerMxid, chatwootBindingId: null, egressProfileId: null, egressPolicy: input.egressPolicy ?? "proxy_required", status: "draft", createdAt: now, updatedAt: now
    };
    this.db.query(`INSERT INTO meta_connections(id, tenant_id, provider, meta_account_id, mautrix_login_id, matrix_owner_mxid, chatwoot_binding_id, egress_profile_id, egress_policy, status, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`).run(connection.id, connection.tenantId, connection.provider, connection.metaAccountId, connection.mautrixLoginId, connection.matrixOwnerMxid, null, null, connection.egressPolicy, connection.status, connection.createdAt, connection.updatedAt);
    return connection;
  }
  findById(id: string): MetaConnection | null {
    const row = this.db.query("SELECT * FROM meta_connections WHERE id = ?").get(id) as Record<string, unknown> | null;
    return row ? connectionFromRow(row) : null;
  }
  findActiveByIdentity(input: { metaAccountId?: string; loginId?: string }): MetaConnection | null {
    if (!input.metaAccountId && !input.loginId) return null;
    const clauses: string[] = [];
    const values: string[] = [];
    if (input.metaAccountId) { clauses.push("mc.meta_account_id = ?"); values.push(input.metaAccountId); }
    if (input.loginId) { clauses.push("mc.mautrix_login_id = ?"); values.push(input.loginId); }
    const rows = this.db.query(`SELECT mc.* FROM meta_connections mc JOIN tenants t ON t.id = mc.tenant_id WHERE t.status = 'active' AND mc.status = 'active' AND (${clauses.join(" OR ")})`).all(...values) as Array<Record<string, unknown>>;
    if (rows.length === 0) return null;
    if (rows.length !== 1) throw new Error("IDENTITY_CONFLICT");
    const connection = connectionFromRow(rows[0]!);
    if (input.metaAccountId && connection.metaAccountId !== null && connection.metaAccountId !== input.metaAccountId) throw new Error("IDENTITY_CONFLICT");
    if (input.loginId && connection.mautrixLoginId !== null && connection.mautrixLoginId !== input.loginId) throw new Error("IDENTITY_CONFLICT");
    return connection;
  }
  assignEgress(connectionId: string, egressProfileId: string): MetaConnection {
    if (!this.db.query("SELECT id FROM egress_profiles WHERE id = ?").get(egressProfileId)) throw new Error("EGRESS_NOT_FOUND");
    const now = new Date().toISOString();
    const result = this.db.query("UPDATE meta_connections SET egress_profile_id = ?, updated_at = ? WHERE id = ?").run(egressProfileId, now, connectionId);
    if (result.changes !== 1) throw new Error("CONNECTION_NOT_FOUND");
    return this.findById(connectionId)!;
  }
  setStatus(connectionId: string, status: ConnectionStatus): MetaConnection {
    const now = new Date().toISOString();
    const result = this.db.query("UPDATE meta_connections SET status = ?, updated_at = ? WHERE id = ?").run(status, now, connectionId);
    if (result.changes !== 1) throw new Error("CONNECTION_NOT_FOUND");
    return this.findById(connectionId)!;
  }
  setProviderIdentity(connectionId: string, input: { metaAccountId?: string; mautrixLoginId?: string }): MetaConnection {
    const current = this.findById(connectionId);
    if (!current) throw new Error("CONNECTION_NOT_FOUND");
    const metaAccountId = input.metaAccountId ?? current.metaAccountId;
    const mautrixLoginId = input.mautrixLoginId ?? current.mautrixLoginId;
    const now = new Date().toISOString();
    this.db.query("UPDATE meta_connections SET meta_account_id = ?, mautrix_login_id = ?, updated_at = ? WHERE id = ?").run(metaAccountId, mautrixLoginId, now, connectionId);
    return this.findById(connectionId)!;
  }
}

export class SQLiteEgressProfileRepository implements EgressProfileRepository {
  constructor(private readonly db: Database) {}
  create(input: Omit<EgressProfile, "id" | "createdAt" | "updatedAt" | "failureCount" | "lastCheckedAt" | "lastVerifiedExitIp">): EgressProfile {
    const now = new Date().toISOString();
    const profile: EgressProfile = { ...input, id: crypto.randomUUID(), lastVerifiedExitIp: null, lastCheckedAt: null, failureCount: 0, createdAt: now, updatedAt: now };
    this.db.query(`INSERT INTO egress_profiles(id, provider, scheme, host, port, username, secret_ref, country, region, sticky_session_id, expected_exit_ip, last_verified_exit_ip, status, last_checked_at, failure_count, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`).run(profile.id, profile.provider, profile.scheme, profile.host, profile.port, profile.username, profile.secretRef, profile.country, profile.region, profile.stickySessionId, profile.expectedExitIp, null, profile.status, null, 0, now, now);
    return profile;
  }
  findById(id: string): EgressProfile | null {
    const row = this.db.query("SELECT * FROM egress_profiles WHERE id = ?").get(id) as Record<string, unknown> | null;
    return row ? egressFromRow(row) : null;
  }
  list(): EgressProfile[] { return (this.db.query("SELECT * FROM egress_profiles ORDER BY created_at, id").all() as Array<Record<string, unknown>>).map(egressFromRow); }
  setStatus(id: string, status: EgressStatus): EgressProfile {
    const now = new Date().toISOString();
    const result = this.db.query("UPDATE egress_profiles SET status = ?, updated_at = ? WHERE id = ?").run(status, now, id);
    if (result.changes !== 1) throw new Error("EGRESS_NOT_FOUND");
    return this.findById(id)!;
  }
}

export class SQLiteAuditRepository implements AuditRepository {
  constructor(private readonly db: Database) {}
  record(input: Omit<AuditEvent, "id" | "createdAt">): AuditEvent {
    const event: AuditEvent = { ...input, id: crypto.randomUUID(), createdAt: new Date().toISOString() };
    this.db.query("INSERT INTO audit_events(id, tenant_id, actor_type, actor_id, action, entity_type, entity_id, before_json, after_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)").run(event.id, event.tenantId, event.actorType, event.actorId, event.action, event.entityType, event.entityId, event.beforeJson, event.afterJson, event.createdAt);
    return event;
  }
  listForEntity(entityType: string, entityId: string): AuditEvent[] {
    return (this.db.query("SELECT * FROM audit_events WHERE entity_type = ? AND entity_id = ? ORDER BY created_at, id").all(entityType, entityId) as Array<Record<string, unknown>>).map(auditFromRow);
  }
}