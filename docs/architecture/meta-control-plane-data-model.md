# Meta Control Plane Data Model and Migration Contract

Status: normative for `dev` integration
Initial engine: `bun:sqlite`
Current schema version: `5`

## Data ownership

The control plane owns tenant configuration, connector metadata, provisioning claims, Matrix room attribution/checkpoints, egress assignments, Chatwoot routing, processed-event idempotency and audit history. It MUST NOT own Meta session cookies or duplicate mautrix session state.

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

### matrix_room_bindings

Schema v4 adds the Matrix-side routing authority required before a Chatwoot conversation exists.

Fields: `matrix_room_id`, `tenant_id`, `meta_connection_id`, `remote_thread_id`, `mautrix_login_id`, `bridge_state_key`, `source_event_id`, `verified_at`, `created_at`, `updated_at`.

Invariants:

- `matrix_room_id` is globally unique in the control-plane database;
- `(meta_connection_id, remote_thread_id)` is unique;
- `tenant_id` MUST equal the referenced connection tenant;
- `mautrix_login_id` MUST equal the current bound login identity of the referenced connection;
- a binding may only be created/reverified from bridge state produced by the configured bridge bot and accepted protocol;
- re-observing the identical room/connection/thread/login association is idempotent and updates verification metadata;
- any contradictory room, connection, remote-thread or login association MUST fail closed rather than mutate ownership silently.

This entity is intentionally separate from `conversation_bindings`: room attribution must be known before the first Chatwoot conversation is created.

### matrix_sync_checkpoints

Schema v4 also stores durable transport progress for each Matrix ingestion consumer.

Fields: `consumer_id`, `next_batch`, `updated_at`.

`consumer_id` is unique. `next_batch` is the Synapse `/sync` continuation token. It is not an idempotency key and MUST NOT replace `processed_events`.

A checkpoint MUST only advance after the corresponding sync batch is handled without a retry-required failure. A limited timeline/gap, malformed Meta-provenance event, attribution conflict or downstream retryable failure MUST leave the previous checkpoint intact.

### egress_profiles

Fields: `id`, `provider`, `scheme`, `host`, `port`, `username`, `secret_ref`, `country`, `region`, `sticky_session_id`, `expected_exit_ip`, `last_verified_exit_ip`, `status`, `last_checked_at`, `failure_count`, `created_at`, `updated_at`.

`egress_profiles` is an operator-managed global registry, not a tenant-owned table. Tenant isolation is expressed by the explicit `meta_connections.egress_profile_id` reservation, not by adding `tenant_id` to a proxy inventory record.

For the current product model, one live connection must have one exclusive egress reservation. Schema v5 enforces that a non-null `egress_profile_id` may be referenced by at most one connection whose status is not `disabled`. `draft`, `ready`, `active`, `degraded` and `blocked` connections therefore continue to reserve their assigned profile. Moving a connection to `disabled` releases that reservation for another connection, while preserving the historical profile reference. A disabled connection cannot become live again while another non-disabled connection has reserved that same profile; the operator must reassign it first.

This exclusivity is deliberate: the first production claim is account-specific stable egress, not a shared NAT pool. A future product decision to permit shared egress would require an explicit policy/schema change and new isolation acceptance tests; it must not emerge accidentally from missing constraints.

A credential-bearing proxy URI MUST NOT be the canonical persisted representation. `secret_ref` points outside the SQLite database to a credential source dedicated to proxy egress. The initial environment-backed provider accepts only references of the form `env:EGRESS_PROXY_*`; it MUST NOT dereference arbitrary process environment variables such as control-plane service tokens.

A proxy username and `secret_ref` MUST either both be configured or both be absent. Scheme, host and port MUST be validated before persistence and again before resolution so malformed persisted configuration cannot become an ambiguous proxy authority.

### chatwoot_bindings

Fields: `id`, `tenant_id`, `chatwoot_account_id`, `chatwoot_inbox_id`, `api_base_url`, `credential_ref`, `status`, `created_at`, `updated_at`.

A binding MUST belong to the same tenant as the Meta connection that references it.

### conversation_bindings

Fields: `id`, `tenant_id`, `meta_connection_id`, `matrix_room_id`, `remote_thread_id`, `remote_contact_id`, `chatwoot_account_id`, `chatwoot_inbox_id`, `chatwoot_contact_id`, `chatwoot_source_id`, `chatwoot_conversation_id`, `created_at`, `updated_at`.

Required uniqueness MUST prevent one provider conversation from being ambiguously mapped to multiple Chatwoot conversations for the same connection.

A `conversation_binding` does not establish Matrix room ownership; when runtime events originate from Synapse, `matrix_room_bindings` is the pre-CRM attribution authority.

### processed_events

Fields: `id`, `source`, `source_event_id`, `meta_connection_id`, `payload_hash`, `status`, `first_seen_at`, `processed_at`, `last_error`.

`(source, source_event_id)` MUST be unique. Processing code MUST use this table to make externally visible side effects replay-safe. Matrix `/sync` checkpointing and processed-event idempotency are complementary and MUST remain separate.

### audit_events

Fields: `id`, `tenant_id`, `actor_type`, `actor_id`, `action`, `entity_type`, `entity_id`, `before_json`, `after_json`, `created_at`.

Changes to egress assignment, connection status, Chatwoot binding and provisioning authority MUST produce audit events. Audit payloads MUST NOT include raw provisioning claims, Meta cookies, Matrix access tokens or secret-bearing proxy URLs.

## Referential invariants

A `meta_connection` MUST NOT reference a Chatwoot binding from another tenant. Egress profiles are intentionally global operator inventory; the required isolation invariant is instead that no two non-disabled connections may reserve the same egress profile under the current product policy. A `conversation_binding` and `matrix_room_binding` MUST match the tenant of their referenced Meta connection. Cross-tenant associations MUST fail at the repository/service boundary and SHOULD also be constrained by foreign keys wherever SQLite permits.

Provider identity ownership is global across connection status. Disabling a connection MUST NOT implicitly free its `meta_account_id` or `mautrix_login_id` for another connection. When multiple supplied identities point to different records, resolution MUST fail closed before active-status filtering.

Provisioning adds a stricter pre-authentication invariant: `claim tenant + claim Matrix owner + connection tenant + connection Matrix owner + submitted c_user` must resolve to one non-conflicting connection. Matrix owner identity alone is never a selector for a Meta connection.

Matrix room attribution adds another independent invariant: `m.bridge channel.receiver` must resolve to one active `mautrix_login_id`; a room name, alias, ghost MXID format or Matrix sender display name MUST NOT select a connection.

## Migration discipline

Migrations MUST be ordered, immutable after merge and applied transactionally where SQLite supports it. Startup MUST either apply all pending migrations successfully or fail readiness; partial schema initialization is not acceptable.

The application records schema version and readiness requires the local database to match `LATEST_SCHEMA_VERSION`.

Migration tests MUST prove fresh-database creation and upgrade from every supported previous schema version used in released deployments. Schema v5 intentionally fails rather than silently choose a winner if a pre-v5 database already contains duplicate live reservations for one egress profile; an operator must resolve that invalid state before accepting the upgrade.

## Repository boundary

Domain/application services SHOULD depend on repository/service boundaries rather than distributing SQLite statements through unrelated runtime code. SQLite adapters are the initial implementation, not the domain contract.

Required persistent boundaries include tenants, Meta connections, provisioning claims, Matrix room bindings, Matrix sync checkpoints, egress profiles, Chatwoot bindings, conversation bindings, processed events and audit events.

## PostgreSQL portability

Avoid SQLite-specific semantics in domain rules. IDs, timestamps, uniqueness, foreign keys and transaction boundaries SHOULD be designed so a PostgreSQL adapter can replace SQLite without changing service behavior. A future PostgreSQL adapter must reproduce the same exclusive-live-egress invariant, even if its constraint/index syntax differs.

## Retention

The MVP may retain audit, consumed provisioning-claim metadata, Matrix room/checkpoint metadata and processed-event metadata indefinitely because expected volume is low, but payload bodies SHOULD be minimized. Raw webhook bodies, Matrix access tokens, raw provisioning secrets, Meta cookies or message contents MUST NOT be retained merely for debugging unless a later retention policy explicitly requires them.

## Required CI proofs

CI MUST test fresh migration, restart persistence, foreign-key/cross-tenant rejection, uniqueness/idempotency constraints, exclusive live egress reservation and safe reuse after disable, concurrent duplicate-event claims, egress secret-reference namespace isolation, provider identity conflict behavior across active/inactive records, provisioning digest-only storage, forged/expired/used/revoked claim rejection, cross-tenant/owner provisioning rejection, same-account re-login behavior, conflicting-account rejection, Matrix room binding idempotency/conflict rules, sync checkpoint persistence/restart behavior, two-tenant room attribution against a disposable Synapse and upgrade behavior across supported schema versions.
