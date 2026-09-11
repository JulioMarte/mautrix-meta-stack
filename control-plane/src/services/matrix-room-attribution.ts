import type {
  MatrixRoomBinding,
  MatrixRoomBindingRepository,
  MetaConnection,
  MetaConnectionRepository
} from "../domain/models";
import type { MatrixRawEvent } from "./matrix-sync-client";

export type BridgeRoomIdentity = {
  remoteThreadId: string;
  mautrixLoginId: string;
  bridgeStateKey: string;
  sourceEventId: string | null;
};

function configuredProtocols(raw: string | undefined): Set<string> {
  const values = (raw ?? "facebookgo").split(",").map((value) => value.trim()).filter(Boolean);
  if (values.length === 0) throw new Error("MATRIX_BRIDGE_PROTOCOL_IDS_INVALID");
  return new Set(values);
}

function stringField(value: unknown): string | null {
  return typeof value === "string" && value.trim() === value && value.length > 0 ? value : null;
}

export class MatrixRoomAttributionService {
  private readonly bridgeBotMxid: string;
  private readonly protocolIds: Set<string>;

  constructor(
    private readonly connections: MetaConnectionRepository,
    private readonly roomBindings: MatrixRoomBindingRepository,
    env: Record<string, string | undefined> = process.env
  ) {
    this.bridgeBotMxid = env.MATRIX_BRIDGE_BOT_MXID ?? "";
    if (!this.bridgeBotMxid.startsWith("@") || !this.bridgeBotMxid.includes(":")) throw new Error("MATRIX_BRIDGE_BOT_MXID_INVALID");
    this.protocolIds = configuredProtocols(env.MATRIX_BRIDGE_PROTOCOL_IDS);
  }

  isTrustedInvite(events: MatrixRawEvent[], syncUserMxid: string): boolean {
    if (!syncUserMxid.startsWith("@") || !syncUserMxid.includes(":")) throw new Error("MATRIX_SYNC_USER_MXID_INVALID");
    let trusted = 0;
    let conflicting = 0;
    for (const event of events) {
      if (event.type !== "m.room.member" || event.state_key !== syncUserMxid) continue;
      if (!event.content || typeof event.content !== "object") continue;
      const membership = stringField((event.content as Record<string, unknown>).membership);
      if (membership !== "invite") continue;
      if (event.sender === this.bridgeBotMxid) trusted++;
      else conflicting++;
    }
    return trusted === 1 && conflicting === 0;
  }

  parseBridgeIdentity(events: MatrixRawEvent[]): BridgeRoomIdentity | null {
    const matches: BridgeRoomIdentity[] = [];
    for (const event of events) {
      if (event.type !== "m.bridge" && event.type !== "uk.half-shot.bridge") continue;
      if (event.sender !== this.bridgeBotMxid) continue;
      const stateKey = stringField(event.state_key);
      if (!stateKey || !event.content || typeof event.content !== "object") continue;
      const content = event.content as Record<string, unknown>;
      if (!content.protocol || typeof content.protocol !== "object" || !content.channel || typeof content.channel !== "object") continue;
      const protocol = content.protocol as Record<string, unknown>;
      const channel = content.channel as Record<string, unknown>;
      const protocolId = stringField(protocol.id);
      const remoteThreadId = stringField(channel.id);
      const mautrixLoginId = stringField(channel.receiver);
      if (!protocolId || !this.protocolIds.has(protocolId) || !remoteThreadId || !mautrixLoginId) continue;
      matches.push({
        remoteThreadId,
        mautrixLoginId,
        bridgeStateKey: stateKey,
        sourceEventId: stringField(event.event_id)
      });
    }
    if (matches.length === 0) return null;
    const first = matches[0]!;
    for (const match of matches.slice(1)) {
      if (
        match.remoteThreadId !== first.remoteThreadId ||
        match.mautrixLoginId !== first.mautrixLoginId ||
        match.bridgeStateKey !== first.bridgeStateKey
      ) throw new Error("MATRIX_BRIDGE_STATE_AMBIGUOUS");
    }
    return first;
  }

  resolveActiveConnection(identity: BridgeRoomIdentity): MetaConnection {
    let connection: MetaConnection | null;
    try {
      connection = this.connections.findActiveByIdentity({ loginId: identity.mautrixLoginId });
    } catch (error) {
      if (error instanceof Error && error.message === "IDENTITY_CONFLICT") throw new Error("MATRIX_BRIDGE_IDENTITY_CONFLICT");
      throw error;
    }
    if (!connection) throw new Error("MATRIX_BRIDGE_CONNECTION_NOT_ACTIVE");
    if (connection.mautrixLoginId !== identity.mautrixLoginId) throw new Error("MATRIX_BRIDGE_IDENTITY_CONFLICT");
    return connection;
  }

  bindRoom(matrixRoomId: string, events: MatrixRawEvent[]): MatrixRoomBinding {
    if (!matrixRoomId.startsWith("!")) throw new Error("MATRIX_ROOM_ID_INVALID");
    const identity = this.parseBridgeIdentity(events);
    if (!identity) throw new Error("MATRIX_BRIDGE_STATE_NOT_FOUND");
    const connection = this.resolveActiveConnection(identity);
    return this.roomBindings.bindVerified({
      matrixRoomId,
      tenantId: connection.tenantId,
      metaConnectionId: connection.id,
      remoteThreadId: identity.remoteThreadId,
      mautrixLoginId: identity.mautrixLoginId,
      bridgeStateKey: identity.bridgeStateKey,
      sourceEventId: identity.sourceEventId
    });
  }
}
