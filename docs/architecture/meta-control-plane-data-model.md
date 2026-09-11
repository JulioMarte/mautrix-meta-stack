# Meta Control Plane Data Model and Migration Contract

Status: normative for `feature/meta-control-plane`
Initial engine: `bun:sqlite`
Current schema version: `3`

## Data ownership

The control plane owns tenant configuration, connector metadata, provisioning claims, egress assignments, Chatwoot routing, processed-event idempotency and audit history. It MUST NOT own Meta session cookies or duplicate mautrix session state.

## Required entities

### tenants

Fields: `id`, `slug`, `name`, `status`, `created_at`, `updated_at`.

`slug` MUST be unique. Tenant deletion is out of scope for the first release; disabling is preferred so routing history remains referentially intact.

### meta_connections

Fields: `id`, `tenant_id`, `provider`, `meta_account_id`, `mautrix_login_id`, `matrix_owner_mxid`, `chatwoot_binding_id`, `egress_profile_id`, `egress_policy`, `status`, `created_at`, `updated_at`.

`meta_account_id`, `mautrix_login_id` and `matrix_owner_mxid` are separate identities even if values happen to correlate.

`egress_policy` is one of `direct_allowed`, `proxy_preferred`, `proxy_required`. Production tenant connections SHOULD default to `proxy_required`. The implemented dynamic provisioning bootstrap requires `proxy_required` because no direct fallback is permitted before the first Meta-bound request.

### provisioning_claims

Schema v3 adds the bootstrap authority used to bind submitted Meta credentials to exactly one pre-created connection before provider traffic begins.

Fields: `id`, `secret_digest`, `meta_connection_id`, `tenant_id`, `matrix_owner_mxid`, `expires_at`, `used_at`, `revoked_at`, `created_at`.

Invariants:

- `secret_digest` is unique and contains a SHA-256 digest of the raw claim; the raw claim MUST NOT be persisted;
- each claim references one `meta_connection` and snapshots its tenant and authorized Matrix owner for conflict checking;
- only unused, unrevoked and unexpired claims can be consumed;
- consuming a claim and binding `meta_account_id` happen in one database transaction;
- issuing a replacement pending claim revokes earlier unused claims for the same connection;
- used claims remain as audit/security history and are not reusable;
- claim rows never contain Meta cookies or proxy credentials.

### egress_profiles

Fields: `id`, `provider`, `scheme`, `host`, `port`, `username`, `secret_ref`, `country`, `region`, `sticky_session_id`, `expected_exit_ip`, `last_verified_exit_ip`, `status`, `last_checked_at`, `failure_count`, `created_at`, `updated_at`.

A credential-bearing proxy URI MUST NOT be the canonical persisted representation. `secret_ref` points outside the SQLite database to a credential source dedicated to proxy egress. The initial environment-backed provider accepts only references of the form `env:EGRESS_PROXY_*`; it MUST NOT dereference arbitrary process environment variables such as control-plane service tokens. This namespace boundary prevents an egress profile from turning unrelated application credentials into outbound proxy authentication material.

A proxy username and `secret_ref` MUST either both be configured or both be absent. Scheme, host and port MUST be validated before persistence and again before resolution so malformed persisted configuration cannot become an ambiguous proxy authority.

### chatwoot_bindings

Fields: `id`, `tenant_id`, `chatwoot_account_id`, `chatwoot_inbox_id`, `api_base_url`, `credential_ref`, `status`, `created_at`, `updated_at`.

A binding MUST belong to the same tenant as the Meta connection that references it.

### conversation_bindings

Fields: `id`, `tenant_id`, `meta_connection_id`, `matrix_room_id`, `remote_thread_id`, `remote_contact_id`, `chatwoot_account_id`, `chatwoot_inbox_id`, `chatwoot_contact_id`, `chatwoot_source_id`, `chatwoot_conversation_id`, `created_at`, `updated_at`.

Required uniqueness MUST prevent one provider conversation from being ambiguously mapped to multiple Chatwoot conversations for the same connection.

### processed_events

Fields: `id`, `source`, `source_event_id`, `meta_connection_id`, `payload_hash`, `status`, `first_seen_at`, `processed_at`, `last_error`.

`(source, source_event_id)` MUST be unique. Processing code MUST use this table to make externally visible side effects replay-safe.

### audit_events

Fields: `id`, `tenant_id`, `actor_type`, `actor_id`, `action`, `entity_type`, `entity_id`, `before_json`, `after_json`, `created_at`.

Changes to egress assignment, connection status, Chatwoot binding and provisioning authority MUST produce audit events. Audit payloads MUST NOT include raw provisioning claims, Meta cookies or secret-bearing proxy URLs.

## Referential invariants

A `meta_connection` MUST NOT reference an egress or Chatwoot binding from another tenant. A `conversation_binding` MUST match the tenant of its referenced Meta connection. Cross-tenant associations MUST fail at the repository/service boundary and SHOULD also be constrained by foreign keys wherever SQLite permits.

Provider identity ownership is global across connection status. Disabling a connection MUST NOT implicitly free its `meta_account_id` or `mautrix_login_id` for another connection. When multiple supplied identities point to different records, resolution MUST fail closed before active-status filtering.

Provisioning adds a stricter pre-authentication invariant: `claim tenant + claim Matrix owner + connection tenant + connection Matrix owner + submitted c_user` must resolve to one non-conflicting connection. Matrix owner identity alone is never a selector for a Meta connection.

## Migration discipline

Migrations MUST be ordered, immutable after merge and applied transactionally where SQLite supports it. Startup MUST either apply all pending migrations successfully or fail readiness; partial schema initialization is not acceptable.

The application records schema version and readiness requires the local database to match `LATEST_SCHEMA_VERSION`.

Migration tests MUST prove fresh-database creation and upgrade from every supported previous schema version used in released deployments.

## Repository boundary

Domain/application services SHOULD depend on repository/service boundaries rather than distributing SQLite statements through unrelated runtime code. SQLite adapters are the initial implementation, not the domain contract. Provisioning is currently isolated in its own Elysia module and transaction boundary rather than mixed into Meta cookie handling.

Required persistent boundaries include tenants, Meta connections, provisioning claims, egress profiles, Chatwoot bindings, conversation bindings, processed events and audit events.

## PostgreSQL portability

Avoid SQLite-specific semantics in domain rules. IDs, timestamps, uniqueness, foreign keys and transaction boundaries SHOULD be designed so a PostgreSQL adapter can replace SQLite without changing service behavior.

## Retention

The MVP may retain audit, consumed provisioning-claim metadata and processed-event metadata indefinitely because expected volume is low, but payload bodies SHOULD be minimized. Raw webhook bodies, raw provisioning secrets, Meta cookies or message contents MUST NOT be retained merely for debugging unless a later retention policy explicitly requires them.

## Required CI proofs

CI MUST test fresh migration, restart persistence, foreign-key/cross-tenant rejection, uniqueness/idempotency constraints, concurrent duplicate-event claims, egress secret-reference namespace isolation, provider identity conflict behavior across active/inactive records, provisioning digest-only storage, forged/expired/used/revoked claim rejection, cross-tenant/owner binding rejection, same-account re-login behavior, conflicting-account rejection and upgrade behavior across supported schema versions.