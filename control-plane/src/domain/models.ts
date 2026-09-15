export type TenantStatus = "active" | "disabled";
export type ConnectionStatus = "draft" | "ready" | "active" | "degraded" | "blocked" | "disabled";
export type EgressPolicy = "direct_allowed" | "proxy_preferred" | "proxy_required";
export type EgressStatus = "healthy" | "degraded" | "disabled";
export type TrafficClass = "login" | "messaging" | "media" | "e2ee";
export type ChatwootBindingStatus = "active" | "disabled";
export type ProcessedEventStatus = "received" | "processing" | "delivered" | "failed_retryable" | "failed_terminal";

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

export type ChatwootBinding = {
  id: string;
  tenantId: string;
  chatwootAccountId: string;
  chatwootInboxId: string;
  apiBaseUrl: string;
  credentialRef: string;
  status: ChatwootBindingStatus;
  createdAt: string;
  updatedAt: string;
};

export type MatrixRoomBinding = {
  matrixRoomId: string;
  tenantId: string;
  metaConnectionId: string;
  remoteThreadId: string;
  mautrixLoginId: string;
  bridgeStateKey: string;
  sourceEventId: string | null;
  verifiedAt: string;
  createdAt: string;
  updatedAt: string;
};

export type MatrixSyncCheckpoint = {
  consumerId: string;
  nextBatch: string;
  updatedAt: string;
};

export type ConversationBinding = {
  id: string;
  tenantId: string;
  metaConnectionId: string;
  matrixRoomId: string;
  remoteThreadId: string;
  remoteContactId: string | null;
  chatwootAccountId: string;
  chatwootInboxId: string;
  chatwootContactId: string | null;
  chatwootSourceId: string | null;
  chatwootConversationId: string;
  createdAt: string;
  updatedAt: string;
};

export type ProcessedEvent = {
  id: string;
  source: "matrix" | "chatwoot";
  sourceEventId: string;
  metaConnectionId: string | null;
  payloadHash: string;
  status: ProcessedEventStatus;
  firstSeenAt: string;
  processedAt: string | null;
  lastError: string | null;
};

export type MatrixEncryptedFile = {
  v: "v2";
  key: {
    kty: "oct";
    alg: "A256CTR";
    k: string;
    keyOps: string[];
    ext: true;
  };
  iv: string;
  hashes: {
    sha256: string;
  };
};

export type Attachment = {
  id?: string;
  kind: "image" | "video" | "audio" | "file" | "unknown";
  url?: string;
  mimeType?: string;
  fileName?: string;
  sizeBytes?: number;
  voiceNote?: boolean;
  encryption?: MatrixEncryptedFile;
};

export type NormalizedMessage = {
  tenantId: string;
  connectionId: string;
  conversationExternalId: string;
  messageExternalId: string;
  senderExternalId: string;
  senderDisplayName?: string;
  direction: "inbound" | "outbound";
  text?: string;
  attachments: Attachment[];
  occurredAt: string;
  source: "matrix" | "chatwoot";
  sourceEventId: string;
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
  setStatus(id: string, status: TenantStatus): Tenant;
}

export interface MetaConnectionRepository {
  create(input: { tenantId: string; matrixOwnerMxid: string; egressPolicy?: EgressPolicy; metaAccountId?: string; mautrixLoginId?: string }): MetaConnection;
  findById(id: string): MetaConnection | null;
  findActiveByIdentity(input: { metaAccountId?: string; loginId?: string }): MetaConnection | null;
  assignEgress(connectionId: string, egressProfileId: string): MetaConnection;
  assignChatwootBinding(connectionId: string, chatwootBindingId: string): MetaConnection;
  setStatus(connectionId: string, status: ConnectionStatus): MetaConnection;
  setProviderIdentity(connectionId: string, input: { metaAccountId?: string; mautrixLoginId?: string }): MetaConnection;
}

export interface EgressProfileRepository {
  create(input: Omit<EgressProfile, "id" | "createdAt" | "updatedAt" | "failureCount" | "lastCheckedAt" | "lastVerifiedExitIp">): EgressProfile;
  findById(id: string): EgressProfile | null;
  list(): EgressProfile[];
  setStatus(id: string, status: EgressStatus): EgressProfile;
}

export interface ChatwootBindingRepository {
  create(input: Omit<ChatwootBinding, "id" | "createdAt" | "updatedAt">): ChatwootBinding;
  findById(id: string): ChatwootBinding | null;
  listForTenant(tenantId: string): ChatwootBinding[];
  setStatus(id: string, status: ChatwootBindingStatus): ChatwootBinding;
}

export interface MatrixRoomBindingRepository {
  bindVerified(input: Omit<MatrixRoomBinding, "verifiedAt" | "createdAt" | "updatedAt">): MatrixRoomBinding;
  findByRoomId(matrixRoomId: string): MatrixRoomBinding | null;
  findByRemoteThread(metaConnectionId: string, remoteThreadId: string): MatrixRoomBinding | null;
  list(): MatrixRoomBinding[];
}

export interface MatrixSyncCheckpointRepository {
  get(consumerId: string): MatrixSyncCheckpoint | null;
  save(consumerId: string, nextBatch: string): MatrixSyncCheckpoint;
}

export interface ConversationBindingRepository {
  create(input: Omit<ConversationBinding, "id" | "createdAt" | "updatedAt">): ConversationBinding;
  findByRemoteThread(metaConnectionId: string, remoteThreadId: string): ConversationBinding | null;
  findByChatwootConversation(input: { tenantId: string; accountId: string; inboxId: string; conversationId: string }): ConversationBinding | null;
}

export interface ProcessedEventRepository {
  claim(input: { source: "matrix" | "chatwoot"; sourceEventId: string; metaConnectionId: string | null; payloadHash: string }): { event: ProcessedEvent; claimed: boolean };
  find(source: "matrix" | "chatwoot", sourceEventId: string): ProcessedEvent | null;
  setStatus(id: string, status: ProcessedEventStatus, lastError?: string | null): ProcessedEvent;
}

export interface AuditRepository {
  record(input: Omit<AuditEvent, "id" | "createdAt">): AuditEvent;
  listForEntity(entityType: string, entityId: string): AuditEvent[];
}

export interface SecretProvider {
  resolve(secretRef: string): string | null;
}
