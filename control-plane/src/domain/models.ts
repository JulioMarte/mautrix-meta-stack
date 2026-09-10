export type TenantStatus = "active" | "disabled";
export type ConnectionStatus = "draft" | "ready" | "active" | "degraded" | "blocked" | "disabled";
export type EgressPolicy = "direct_allowed" | "proxy_preferred" | "proxy_required";
export type EgressStatus = "healthy" | "degraded" | "disabled";
export type TrafficClass = "login" | "messaging" | "media" | "e2ee";

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

export type EgressProfile = {
  id: string;
  provider: string;
  scheme: string;
  host: string;
  port: number;
  username: string | null;
  secretRef: string | null;
  country: string | null;
  region: string | null;
  stickySessionId: string | null;
  expectedExitIp: string | null;
  lastVerifiedExitIp: string | null;
  status: EgressStatus;
  lastCheckedAt: string | null;
  failureCount: number;
  createdAt: string;
  updatedAt: string;
};

export type AuditEvent = {
  id: string;
  tenantId: string | null;
  actorType: string;
  actorId: string;
  action: string;
  entityType: string;
  entityId: string;
  beforeJson: string | null;
  afterJson: string | null;
  createdAt: string;
};

export interface TenantRepository {
  create(input: { slug: string; name: string }): Tenant;
  findById(id: string): Tenant | null;
  list(): Tenant[];
}

export interface MetaConnectionRepository {
  create(input: { tenantId: string; matrixOwnerMxid: string; egressPolicy?: EgressPolicy; metaAccountId?: string; mautrixLoginId?: string }): MetaConnection;
  findById(id: string): MetaConnection | null;
  findActiveByIdentity(input: { metaAccountId?: string; loginId?: string }): MetaConnection | null;
  assignEgress(connectionId: string, egressProfileId: string): MetaConnection;
  setStatus(connectionId: string, status: ConnectionStatus): MetaConnection;
  setProviderIdentity(connectionId: string, input: { metaAccountId?: string; mautrixLoginId?: string }): MetaConnection;
}

export interface EgressProfileRepository {
  create(input: Omit<EgressProfile, "id" | "createdAt" | "updatedAt" | "failureCount" | "lastCheckedAt" | "lastVerifiedExitIp">): EgressProfile;
  findById(id: string): EgressProfile | null;
  list(): EgressProfile[];
  setStatus(id: string, status: EgressStatus): EgressProfile;
}

export interface AuditRepository {
  record(input: Omit<AuditEvent, "id" | "createdAt">): AuditEvent;
  listForEntity(entityType: string, entityId: string): AuditEvent[];
}

export interface SecretProvider {
  resolve(secretRef: string): string | null;
}

export interface ChatwootBindingRepository {}
export interface ConversationBindingRepository {}
export interface ProcessedEventRepository {}
