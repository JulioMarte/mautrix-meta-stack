export type TenantStatus = "active" | "disabled";
export type ConnectionStatus = "draft" | "ready" | "active" | "degraded" | "blocked" | "disabled";
export type EgressPolicy = "direct_allowed" | "proxy_preferred" | "proxy_required";

export type Tenant = {
  id: string;
  slug: string;
  name: string;
  status: TenantStatus;
  createdAt: string;
  updatedAt: string;
};

export type MetaConnection = {
  id: string;
  tenantId: string;
  provider: "facebook";
  metaAccountId: string | null;
  mautrixLoginId: string | null;
  matrixOwnerMxid: string;
  chatwootBindingId: string | null;
  egressProfileId: string | null;
  egressPolicy: EgressPolicy;
  status: ConnectionStatus;
  createdAt: string;
  updatedAt: string;
};

export interface TenantRepository {
  create(input: { slug: string; name: string }): Tenant;
  findById(id: string): Tenant | null;
  list(): Tenant[];
}

export interface MetaConnectionRepository {
  create(input: { tenantId: string; matrixOwnerMxid: string; egressPolicy?: EgressPolicy }): MetaConnection;
  findById(id: string): MetaConnection | null;
}

export interface EgressProfileRepository {}
export interface ChatwootBindingRepository {}
export interface ConversationBindingRepository {}
export interface ProcessedEventRepository {}
export interface AuditRepository {}
