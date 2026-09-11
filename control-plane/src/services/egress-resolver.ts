import type { EgressProfileRepository, MetaConnectionRepository, SecretProvider, TrafficClass } from "../domain/models";

export type ResolveInput = { metaAccountId?: string; loginId?: string; reason: string; trafficClass: TrafficClass };
export type ResolveResult = { proxyUrl: string; assignmentId: string; connectionId: string };
export type ResolverErrorCode = "IDENTITY_REQUIRED" | "IDENTITY_CONFLICT" | "CONNECTION_NOT_ACTIVE" | "EGRESS_ASSIGNMENT_REQUIRED" | "EGRESS_UNHEALTHY" | "EGRESS_SECRET_MISSING" | "EGRESS_CONFIGURATION_INVALID" | "DIRECT_EGRESS_NOT_SUPPORTED";

const supportedProxySchemes = new Set(["http", "https", "socks5"]);

export class ResolverError extends Error {
  constructor(public readonly code: ResolverErrorCode) { super(code); }
}

export class EgressResolver {
  constructor(private readonly connections: MetaConnectionRepository, private readonly egress: EgressProfileRepository, private readonly secrets: SecretProvider) {}

  resolve(input: ResolveInput): ResolveResult {
    if (!input.metaAccountId && !input.loginId) throw new ResolverError("IDENTITY_REQUIRED");
    let connection;
    try { connection = this.connections.findActiveByIdentity(input); }
    catch (error) { if (error instanceof Error && error.message === "IDENTITY_CONFLICT") throw new ResolverError("IDENTITY_CONFLICT"); throw error; }
    if (!connection) throw new ResolverError("CONNECTION_NOT_ACTIVE");
    if (connection.egressPolicy === "direct_allowed") throw new ResolverError("DIRECT_EGRESS_NOT_SUPPORTED");
    if (!connection.egressProfileId) throw new ResolverError("EGRESS_ASSIGNMENT_REQUIRED");
    const profile = this.egress.findById(connection.egressProfileId);
    if (!profile) throw new ResolverError("EGRESS_ASSIGNMENT_REQUIRED");
    if (profile.status !== "healthy") throw new ResolverError("EGRESS_UNHEALTHY");
    if (!supportedProxySchemes.has(profile.scheme.toLowerCase()) || !profile.host || profile.port < 1 || profile.port > 65535) throw new ResolverError("EGRESS_CONFIGURATION_INVALID");

    let auth = "";
    if (profile.secretRef) {
      const secret = this.secrets.resolve(profile.secretRef);
      if (!secret) throw new ResolverError("EGRESS_SECRET_MISSING");
      if (!profile.username) throw new ResolverError("EGRESS_CONFIGURATION_INVALID");
      auth = `${encodeURIComponent(profile.username)}:${encodeURIComponent(secret)}@`;
    } else if (profile.username) {
      throw new ResolverError("EGRESS_CONFIGURATION_INVALID");
    }
    return { proxyUrl: `${profile.scheme.toLowerCase()}://${auth}${profile.host}:${profile.port}`, assignmentId: profile.id, connectionId: connection.id };
  }
}
