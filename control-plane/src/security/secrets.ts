import type { SecretProvider } from "../domain/models";

function resolveEnvRef(pattern: RegExp, secretRef: string, env: Record<string, string | undefined>): string | null {
  const match = pattern.exec(secretRef);
  if (!match) return null;
  const value = env[match[1]!];
  return value && value.length > 0 ? value : null;
}

// Persisted egress references may only dereference variables explicitly dedicated
// to proxy credentials. Unrelated control-plane tokens must never become proxy auth.
const EGRESS_ENV_REF = /^env:(EGRESS_PROXY_[A-Z0-9_]*)$/;
const CHATWOOT_ENV_REF = /^env:(CHATWOOT_[A-Z0-9_]*)$/;

export class EnvironmentSecretProvider implements SecretProvider {
  constructor(private readonly env: Record<string, string | undefined> = process.env) {}
  resolve(secretRef: string): string | null { return resolveEnvRef(EGRESS_ENV_REF, secretRef, this.env); }
}

export class ChatwootEnvironmentSecretProvider implements SecretProvider {
  constructor(private readonly env: Record<string, string | undefined> = process.env) {}
  resolve(secretRef: string): string | null { return resolveEnvRef(CHATWOOT_ENV_REF, secretRef, this.env); }
}

export function isSupportedSecretRef(value: string): boolean { return EGRESS_ENV_REF.test(value); }
export function isSupportedChatwootSecretRef(value: string): boolean { return CHATWOOT_ENV_REF.test(value); }

export function redactUri(value: string): string {
  try {
    const url = new URL(value);
    if (url.username) url.username = "***";
    if (url.password) url.password = "***";
    return url.toString();
  } catch {
    return "[redacted]";
  }
}
