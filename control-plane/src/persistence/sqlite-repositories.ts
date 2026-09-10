import type { Database } from "bun:sqlite";
import type { EgressPolicy, MetaConnection, MetaConnectionRepository, Tenant, TenantRepository } from "../domain/models";

function tenantFromRow(row: Record<string, unknown>): Tenant {
  return {
    id: String(row.id), slug: String(row.slug), name: String(row.name), status: row.status as Tenant["status"],
    createdAt: String(row.created_at), updatedAt: String(row.updated_at)
  };
}

function connectionFromRow(row: Record<string, unknown>): MetaConnection {
  return {
    id: String(row.id), tenantId: String(row.tenant_id), provider: "facebook",
    metaAccountId: row.meta_account_id == null ? null : String(row.meta_account_id),
    mautrixLoginId: row.mautrix_login_id == null ? null : String(row.mautrix_login_id),
    matrixOwnerMxid: String(row.matrix_owner_mxid),
    chatwootBindingId: row.chatwoot_binding_id == null ? null : String(row.chatwoot_binding_id),
    egressProfileId: row.egress_profile_id == null ? null : String(row.egress_profile_id),
    egressPolicy: row.egress_policy as EgressPolicy, status: row.status as MetaConnection["status"],
    createdAt: String(row.created_at), updatedAt: String(row.updated_at)
  };
}

export class SQLiteTenantRepository implements TenantRepository {
  constructor(private readonly db: Database) {}
  create(input: { slug: string; name: string }): Tenant {
    const now = new Date().toISOString();
    const tenant: Tenant = { id: crypto.randomUUID(), slug: input.slug, name: input.name, status: "active", createdAt: now, updatedAt: now };
    this.db.query("INSERT INTO tenants(id, slug, name, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)")
      .run(tenant.id, tenant.slug, tenant.name, tenant.status, tenant.createdAt, tenant.updatedAt);
    return tenant;
  }
  findById(id: string): Tenant | null {
    const row = this.db.query("SELECT * FROM tenants WHERE id = ?").get(id) as Record<string, unknown> | null;
    return row ? tenantFromRow(row) : null;
  }
  list(): Tenant[] {
    return (this.db.query("SELECT * FROM tenants ORDER BY created_at, id").all() as Array<Record<string, unknown>>).map(tenantFromRow);
  }
}

export class SQLiteMetaConnectionRepository implements MetaConnectionRepository {
  constructor(private readonly db: Database) {}
  create(input: { tenantId: string; matrixOwnerMxid: string; egressPolicy?: EgressPolicy }): MetaConnection {
    const tenant = this.db.query("SELECT id FROM tenants WHERE id = ? AND status = 'active'").get(input.tenantId);
    if (!tenant) throw new Error("TENANT_NOT_ACTIVE");
    const now = new Date().toISOString();
    const connection: MetaConnection = {
      id: crypto.randomUUID(), tenantId: input.tenantId, provider: "facebook", metaAccountId: null, mautrixLoginId: null,
      matrixOwnerMxid: input.matrixOwnerMxid, chatwootBindingId: null, egressProfileId: null,
      egressPolicy: input.egressPolicy ?? "proxy_required", status: "draft", createdAt: now, updatedAt: now
    };
    this.db.query(`INSERT INTO meta_connections(id, tenant_id, provider, meta_account_id, mautrix_login_id, matrix_owner_mxid, chatwoot_binding_id, egress_profile_id, egress_policy, status, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
      .run(connection.id, connection.tenantId, connection.provider, null, null, connection.matrixOwnerMxid, null, null, connection.egressPolicy, connection.status, connection.createdAt, connection.updatedAt);
    return connection;
  }
  findById(id: string): MetaConnection | null {
    const row = this.db.query("SELECT * FROM meta_connections WHERE id = ?").get(id) as Record<string, unknown> | null;
    return row ? connectionFromRow(row) : null;
  }
}
