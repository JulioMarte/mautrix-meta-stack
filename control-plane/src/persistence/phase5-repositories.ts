import type { Database } from "bun:sqlite";
import type { ChatwootBinding } from "../domain/models";

export type ChatwootWebhookConfig = {
  bindingId: string;
  secretRef: string;
  updatedAt: string;
};

export class SQLiteChatwootWebhookConfigRepository {
  constructor(private readonly db: Database) {}

  find(bindingId: string): ChatwootWebhookConfig | null {
    const row = this.db.query("SELECT binding_id, secret_ref, updated_at FROM chatwoot_webhook_configs WHERE binding_id = ?").get(bindingId) as { binding_id: string; secret_ref: string; updated_at: string } | null;
    return row ? { bindingId: row.binding_id, secretRef: row.secret_ref, updatedAt: row.updated_at } : null;
  }

  set(bindingId: string, secretRef: string): ChatwootWebhookConfig {
    const exists = this.db.query("SELECT id FROM chatwoot_bindings WHERE id = ?").get(bindingId);
    if (!exists) throw new Error("CHATWOOT_BINDING_NOT_FOUND");
    const now = new Date().toISOString();
    this.db.query(`INSERT INTO chatwoot_webhook_configs(binding_id, secret_ref, updated_at) VALUES (?, ?, ?)
      ON CONFLICT(binding_id) DO UPDATE SET secret_ref = excluded.secret_ref, updated_at = excluded.updated_at`).run(bindingId, secretRef, now);
    return this.find(bindingId)!;
  }
}

export class SQLitePhase5LookupRepository {
  constructor(private readonly db: Database) {}

  findChatwootBinding(bindingId: string): ChatwootBinding | null {
    const row = this.db.query("SELECT * FROM chatwoot_bindings WHERE id = ?").get(bindingId) as Record<string, unknown> | null;
    if (!row) return null;
    return {
      id: String(row.id),
      tenantId: String(row.tenant_id),
      chatwootAccountId: String(row.chatwoot_account_id),
      chatwootInboxId: String(row.chatwoot_inbox_id),
      apiBaseUrl: String(row.api_base_url),
      credentialRef: String(row.credential_ref),
      status: row.status as ChatwootBinding["status"],
      createdAt: String(row.created_at),
      updatedAt: String(row.updated_at)
    };
  }
}
