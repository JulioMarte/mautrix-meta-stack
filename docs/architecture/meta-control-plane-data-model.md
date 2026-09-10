# Meta Control Plane Data Model and Migration Contract

Status: normative for `feature/meta-control-plane`
Initial engine: `bun:sqlite`

## Data ownership

The control plane owns tenant configuration, connector metadata, egress assignments, Chatwoot routing, processed-event idempotency and audit history. It MUST NOT own Meta session cookies or duplicate mautrix session state.

## Required entities

### tenants

Fields: `id`, `slug`, `name`, `status`, `created_at`, `updated_at`.

`slug` MUST be unique. Tenant deletion is out of scope for the first release; disabling is preferred so routing history remains referentially intact.

### meta_connections

Fields: `id`, `tenant_id`, `provider`, `meta_account_id`, `mautrix_login_id`, `matrix_owner_mxid`, `chatwoot_binding_id`, `egress_profile_id`, `egress_policy`, `status`, `created_at`, `updated_at`.

`meta_account_id`, `mautrix_login_id` and `matrix_owner_mxid` are separate identities even if values happen to correlate.

`egress_policy` is one of `direct_allowed`, `proxy_preferred`, `proxy_required`. Production tenant connections SHOULD default to `proxy_required`.

### egress_profiles

Fields: `id`, `provider`, `scheme`, `host`, `port`, `username`, `secret_ref`, `country`, `region`, `sticky_session_id`, `expected_exit_ip`, `last_verified_exit_ip`, `status`, `last_checked_at`, `failure_count`, `created_at`, `updated_at`.

A credential-bearing proxy URI MUST NOT be the canonical persisted representation. `secret_ref` points outside the SQLite database to the credential source.

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

Changes to egress assignment, connection status and Chatwoot binding MUST produce audit events.

## Referential invariants

A `meta_connection` MUST NOT reference an egress or Chatwoot binding from another tenant. A `conversation_binding` MUST match the tenant of its referenced Meta connection. Cross-tenant associations MUST fail at the repository/service boundary and SHOULD also be constrained by foreign keys wherever SQLite permits.

## Migration discipline

Migrations MUST be ordered, immutable after merge and applied transactionally where SQLite supports it. Startup MUST either apply all pending migrations successfully or fail readiness; partial schema initialization is not acceptable.

The application MUST record schema version and expose enough readiness information to distinguish migration failure from runtime dependency failure.

Migration tests MUST prove fresh-database creation and upgrade from every supported previous schema version used in released deployments.

## Repository boundary

Domain/application services MUST depend on repository interfaces rather than issuing SQLite statements throughout the codebase. SQLite adapters are the initial implementation, not the domain contract.

Required repositories: tenants, Meta connections, egress profiles, Chatwoot bindings, conversation bindings, processed events and audit events.

## PostgreSQL portability

Avoid SQLite-specific semantics in domain rules. IDs, timestamps, uniqueness, foreign keys and transaction boundaries SHOULD be designed so a PostgreSQL adapter can replace SQLite without changing service behavior.

## Retention

The MVP may retain audit and processed-event metadata indefinitely because expected volume is low, but payload bodies SHOULD be minimized. Raw webhook bodies or message contents MUST NOT be retained merely for debugging unless a later retention policy explicitly requires them.

## Required CI proofs

CI MUST test fresh migration, restart persistence, foreign-key/cross-tenant rejection, uniqueness/idempotency constraints, concurrent duplicate-event claims, and upgrade behavior once a second schema version exists.