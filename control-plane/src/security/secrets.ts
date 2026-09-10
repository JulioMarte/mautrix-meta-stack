import type { SecretProvider } from "../domain/models";

const ENV_REF = /^env:([A-Z][A-Z0-9_]*)$/;

export class EnvironmentSecretProvider implements SecretProvider {
  constructor(private readonly env: Record<string, string | undefined> = process.env) {}
  resolve(secretRef: string): string | null {
    const match = ENV_REF.exec(secretRef);
    if (!match) return null;
    const value = this.env[match[1]!];
    return value && value.length > 0 ? value : null;
  }
}

export function isSupportedSecretRef(value: string): boolean { return ENV_REF.test(value); }

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
