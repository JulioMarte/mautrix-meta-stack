import type { Database } from "bun:sqlite";
import type {
  AuditEvent, AuditRepository, ChatwootBinding, ChatwootBindingRepository, ChatwootBindingStatus,
  ConnectionStatus, ConversationBinding, ConversationBindingRepository, EgressPolicy, EgressProfile,
  EgressProfileRepository, EgressStatus, MetaConnection, MetaConnectionRepository, ProcessedEvent,
  ProcessedEventRepository, ProcessedEventStatus, Tenant, TenantRepository, TenantStatus
} from "../domain/models";

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

function chatwootBindingFromRow(row: Record<string, unknown>): ChatwootBinding {
  return {
    id: String(row.id), tenantId: String(row.tenant_id), chatwootAccountId: String(row.chatwoot_account_id),
    chatwootInboxId: String(row.chatwoot_inbox_id), apiBaseUrl: String(row.api_base_url), credentialRef: String(row.credential_ref),
    status: row.status as ChatwootBindingStatus, createdAt: String(row.created_at), updatedAt: String(row.updated_at)
  };
}

function conversationBindingFromRow(row: Record<string, unknown>): ConversationBinding {
  return {
    id: String(row.id), tenantId: String(row.tenant_id), metaConnectionId: String(row.meta_connection_id), matrixRoomId: String(row.matrix_room_id),
    remoteThreadId: String(row.remote_thread_id), remoteContactId: row.remote_contact_id == null ? null : String(row.remote_contact_id),
    chatwootAccountId: String(row.chatwoot_account_id), chatwootInboxId: String(row.chatwoot_inbox_id),
    chatwootContactId: row.chatwoot_contact_id == null ? null : String(row.chatwoot_contact_id),
    chatwootSourceId: row.chatwoot_source_id == null ? null : String(row.chatwoot_source_id), chatwootConversationId: String(row.chatwoot_conversation_id),
    createdAt: String(row.created_at), updatedAt: String(row.updated_at)
  };
}

function processedEventFromRow(row: Record<string, unknown>): ProcessedEvent {
  return {
    id: String(row.id), source: row.source as ProcessedEvent["source"], sourceEventId: String(row.source_event_id),
    metaConnectionId: row.meta_connection_id == null ? null : String(row.meta_connection_id), payloadHash: String(row.payload_hash),
    status: row.status as ProcessedEventStatus, firstSeenAt: String(row.first_seen_at),
    processedAt: row.processed_at == null ? null : String(row.processed_at), lastError: row.last_error == null ? null : String(row.last_error)
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
  setStatus(id: string, status: TenantStatus): Tenant {
    const now = new Date().toISOString();
    const result = this.db.query("UPDATE tenants SET status = ?, updated_at = ? WHERE id = ?").run(status, now, id);
    if (result.changes !== 1) throw new Error("TENANT_NOT_FOUND");
    return this.findById(id)!;
  }
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
    const rows = this.db.query(`SELECT mc.*, t.status AS tenant_status FROM meta_connections mc JOIN tenants t ON t.id = mc.tenant_id WHERE (${clauses.join(" OR ")})`).all(...values) as Array<Record<string, unknown>>;
    if (rows.length === 0) return null;
    if (rows.length !== 1) throw new Error("IDENTITY_CONFLICT");
    const row = rows[0]!;
    const connection = connectionFromRow(row);
    if (input.metaAccountId && connection.metaAccountId !== null && connection.metaAccountId !== input.metaAccountId) throw new Error("IDENTITY_CONFLICT");
    if (input.loginId && connection.mautrixLoginId !== null && connection.mautrixLoginId !== input.loginId) throw new Error("IDENTITY_CONFLICT");
    if (row.tenant_status !== "active" || connection.status !== "active") return null;
    return connection;
  }
  assignEgress(connectionId: string, egressProfileId: string): MetaConnection {
    if (!this.db.query("SELECT id FROM egress_profiles WHERE id = ?").get(egressProfileId)) throw new Error("EGRESS_NOT_FOUND");
    const now = new Date().toISOString();
    const result = this.db.query("UPDATE meta_connections SET egress_profile_id = ?, updated_at = ? WHERE id = ?").run(egressProfileId, now, connectionId);
    if (result.changes !== 1) throw new Error("CONNECTION_NOT_FOUND");
    return this.findById(connectionId)!;
  }
  assignChatwootBinding(connectionId: string, chatwootBindingId: string): MetaConnection {
    const row = this.db.query(`SELECT mc.tenant_id AS connection_tenant, cb.tenant_id AS binding_tenant
      FROM meta_connections mc JOIN chatwoot_bindings cb ON cb.id = ? WHERE mc.id = ?`).get(chatwootBindingId, connectionId) as { connection_tenant: string; binding_tenant: string } | null;
    if (!row) throw new Error("CHATWOOT_BINDING_NOT_FOUND");
    if (row.connection_tenant !== row.binding_tenant) throw new Error("CROSS_TENANT_CHATWOOT_BINDING");
    const now = new Date().toISOString();
    this.db.query("UPDATE meta_connections SET chatwoot_binding_id = ?, updated_at = ? WHERE id = ?").run(chatwootBindingId, now, connectionId);
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

export class SQLiteChatwootBindingRepository implements ChatwootBindingRepository {
  constructor(private readonly db: Database) {}
  create(input: Omit<ChatwootBinding, "id" | "createdAt" | "updatedAt">): ChatwootBinding {
    if (!this.db.query("SELECT id FROM tenants WHERE id = ? AND status = 'active'").get(input.tenantId)) throw new Error("TENANT_NOT_ACTIVE");
    const now = new Date().toISOString();
    const binding: ChatwootBinding = { ...input, id: crypto.randomUUID(), createdAt: now, updatedAt: now };
    this.db.query(`INSERT INTO chatwoot_bindings(id, tenant_id, chatwoot_account_id, chatwoot_inbox_id, api_base_url, credential_ref, status, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`).run(binding.id, binding.tenantId, binding.chatwootAccountId, binding.chatwootInboxId, binding.apiBaseUrl, binding.credentialRef, binding.status, now, now);
    return binding;
  }
  findById(id: string): ChatwootBinding | null {
    const row = this.db.query("SELECT * FROM chatwoot_bindings WHERE id = ?").get(id) as Record<string, unknown> | null;
    return row ? chatwootBindingFromRow(row) : null;
  }
  listForTenant(tenantId: string): ChatwootBinding[] {
    return (this.db.query("SELECT * FROM chatwoot_bindings WHERE tenant_id = ? ORDER BY created_at, id").all(tenantId) as Array<Record<string, unknown>>).map(chatwootBindingFromRow);
  }
  setStatus(id: string, status: ChatwootBindingStatus): ChatwootBinding {
    const now = new Date().toISOString();
    const result = this.db.query("UPDATE chatwoot_bindings SET status = ?, updated_at = ? WHERE id = ?").run(status, now, id);
    if (result.changes !== 1) throw new Error("CHATWOOT_BINDING_NOT_FOUND");
    return this.findById(id)!;
  }
}

export class SQLiteConversationBindingRepository implements ConversationBindingRepository {
  constructor(private readonly db: Database) {}
  create(input: Omit<ConversationBinding, "id" | "createdAt" | "updatedAt">): ConversationBinding {
    const route = this.db.query(`SELECT mc.tenant_id, mc.chatwoot_binding_id, cb.chatwoot_account_id, cb.chatwoot_inbox_id, cb.status AS binding_status
      FROM meta_connections mc LEFT JOIN chatwoot_bindings cb ON cb.id = mc.chatwoot_binding_id WHERE mc.id = ?`).get(input.metaConnectionId) as Record<string, unknown> | null;
    if (!route) throw new Error("CONNECTION_NOT_FOUND");
    if (String(route.tenant_id) !== input.tenantId) throw new Error("CROSS_TENANT_CONVERSATION_BINDING");
    if (route.chatwoot_binding_id == null || route.binding_status !== "active") throw new Error("CHATWOOT_BINDING_NOT_ACTIVE");
    if (String(route.chatwoot_account_id) !== input.chatwootAccountId || String(route.chatwoot_inbox_id) !== input.chatwootInboxId) throw new Error("CHATWOOT_ROUTE_MISMATCH");
    const now = new Date().toISOString();
    const binding: ConversationBinding = { ...input, id: crypto.randomUUID(), createdAt: now, updatedAt: now };
    this.db.query(`INSERT INTO conversation_bindings(id, tenant_id, meta_connection_id, matrix_room_id, remote_thread_id, remote_contact_id, chatwoot_account_id, chatwoot_inbox_id, chatwoot_contact_id, chatwoot_source_id, chatwoot_conversation_id, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`).run(binding.id, binding.tenantId, binding.metaConnectionId, binding.matrixRoomId, binding.remoteThreadId, binding.remoteContactId, binding.chatwootAccountId, binding.chatwootInboxId, binding.chatwootContactId, binding.chatwootSourceId, binding.chatwootConversationId, now, now);
    return binding;
  }
  findByRemoteThread(metaConnectionId: string, remoteThreadId: string): ConversationBinding | null {
    const row = this.db.query("SELECT * FROM conversation_bindings WHERE meta_connection_id = ? AND remote_thread_id = ?").get(metaConnectionId, remoteThreadId) as Record<string, unknown> | null;
    return row ? conversationBindingFromRow(row) : null;
  }
  findByChatwootConversation(input: { tenantId: string; accountId: string; inboxId: string; conversationId: string }): ConversationBinding | null {
    const row = this.db.query(`SELECT * FROM conversation_bindings WHERE tenant_id = ? AND chatwoot_account_id = ? AND chatwoot_inbox_id = ? AND chatwoot_conversation_id = ?`).get(input.tenantId, input.accountId, input.inboxId, input.conversationId) as Record<string, unknown> | null;
    return row ? conversationBindingFromRow(row) : null;
  }
}

export class SQLiteProcessedEventRepository implements ProcessedEventRepository {
  constructor(private readonly db: Database) {}
  claim(input: { source: "matrix" | "chatwoot"; sourceEventId: string; metaConnectionId: string | null; payloadHash: string }): { event: ProcessedEvent; claimed: boolean } {
    const now = new Date().toISOString();
    const id = crypto.randomUUID();
    const result = this.db.query(`INSERT OR IGNORE INTO processed_events(id, source, source_event_id, meta_connection_id, payload_hash, status, first_seen_at, processed_at, last_error)
      VALUES (?, ?, ?, ?, ?, 'received', ?, NULL, NULL)`).run(id, input.source, input.sourceEventId, input.metaConnectionId, input.payloadHash, now);
    const event = this.find(input.source, input.sourceEventId)!;
    if (event.payloadHash !== input.payloadHash || event.metaConnectionId !== input.metaConnectionId) throw new Error("EVENT_IDENTITY_CONFLICT");
    return { event, claimed: result.changes === 1 };
  }
  find(source: "matrix" | "chatwoot", sourceEventId: string): ProcessedEvent | null {
    const row = this.db.query("SELECT * FROM processed_events WHERE source = ? AND source_event_id = ?").get(source, sourceEventId) as Record<string, unknown> | null;
    return row ? processedEventFromRow(row) : null;
  }
  setStatus(id: string, status: ProcessedEventStatus, lastError: string | null = null): ProcessedEvent {
    const processedAt = status === "delivered" || status === "failed_terminal" ? new Date().toISOString() : null;
    const result = this.db.query("UPDATE processed_events SET status = ?, processed_at = ?, last_error = ? WHERE id = ?").run(status, processedAt, lastError, id);
    if (result.changes !== 1) throw new Error("PROCESSED_EVENT_NOT_FOUND");
    const row = this.db.query("SELECT * FROM processed_events WHERE id = ?").get(id) as Record<string, unknown> | null;
    return processedEventFromRow(row!);
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
