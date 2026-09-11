# Implementation Discoveries

Status: operational record. Normative architecture remains in the subsystem contracts; when a discovery changes a contract, update the normative document as well.

## 2026-09-10 — Repository and upstream lineage

The repository `JulioMarte/mautrix-meta-stack` remains a GitHub fork of `mautrix/meta` even though the stack-oriented `main`/`dev` tree no longer resembles upstream source layout. GitHub therefore still provides direct ancestry and upstream object access.

The exact Phase 3 baseline is recoverable and pinned as:

```text
mautrix/meta v0.2607.0
commit ed37c9e6ce47e83dc75b9abea7b636302715b9bc
reference branch upstream/mautrix-meta-v0.2607.0
```

Implication: do not reconstruct the mautrix baseline by copying files from an arbitrary current checkout. Every fork change must remain mechanically comparable to this exact object.

## 2026-09-10 — Development integration branch

`dev` is the primary integration branch. `main` is deployment-triggering state, not the place where active phases accumulate. All normal feature/fix/docs PRs target `dev`; promotion to `main` is deliberate and only after a green deployable checkpoint.

Implication: CI status on `main` does not prove an unmerged phase. The exact phase candidate SHA and the post-merge `dev` SHA both matter.

## 2026-09-10 — CI is the executable authority for this workflow

The current assistant execution environment does not provide a reliable local Bun toolchain/networked dependency installation path. The repository contract already requires exact-SHA CI proof, so executable claims must be based on GitHub Actions rather than inferred from static review.

Implication: never call a phase complete because code type-checks conceptually or because an in-memory test was written. Observe CI on the candidate commit and, after merge, on `dev`.

## 2026-09-10 — Docker named-volume ownership

A newly created Docker named volume mounted at `/data` is root-owned by default in the tested CI topology. Running the control-plane image as the unprivileged `bun` user therefore initially produced:

```text
SQLiteError: unable to open database file
```

The accepted pattern is a one-shot root init service that prepares/chowns the volume followed by the long-running control-plane process as non-root.

Implication: do not solve persistent-volume permission failures by running the application permanently as root. Keep privilege elevation confined to deterministic initialization.

## 2026-09-10 — Host/runner environment variables are not container environment variables

Declaring a canary secret in GitHub Actions job `env` makes it available to the runner and Compose interpolation, but it does not automatically place that variable inside a container unless the Compose service explicitly maps it.

The first Phase 2 real-container resolver test exposed this difference: unit tests succeeded, while the container resolver failed closed because the referenced `env:` secret was absent inside the application container.

Implication: production Compose must declare only production-required secret inputs. CI-only fixture secrets belong in a CI-specific Compose override so test credentials are injected intentionally without polluting the production service contract.

## 2026-09-10 — In-memory success is insufficient for resolver behavior

Phase 2 unit/contract tests proved sticky lookup, fail-closed logic and authentication, but the first real-container test still found a secret-injection wiring defect.

Implication: preserve the split between domain tests and real-container integration tests. Do not replace either with the other.

## 2026-09-10 — Security-sensitive state plus audit must be atomic

Egress assignment and connection activation are security-sensitive state transitions whose audit event is part of the required behavior. Writing the state and audit event separately permits an unaudited committed change if the second write fails.

Implication: perform the state transition and its audit record in one persistence transaction whenever they share the same datastore.

## 2026-09-10 — Authentication secrets should not use ordinary string equality

Internal/admin bearer tokens are long-lived service/operator credentials. Ordinary equality is simple but provides avoidable timing variation.

Implication: compare fixed expected credentials using a length check plus constant-time byte comparison. Authentication failures remain generic and must not disclose resource existence.

## 2026-09-10 — mautrix media transport is global mutable state upstream

Upstream `pkg/msgconv/mediadl/download.go` owns one process-global `mediaHTTPClient`, and `SetProxy()` mutates that client's `http.Transport.Proxy`. That design is safe for one connector-global proxy but is unsafe as a per-account mechanism: two concurrent tenant downloads could overwrite the shared proxy selection and cross egress boundaries.

Implication: never implement per-account media routing by calling the existing global `SetProxy()` before each download. Account-specific media egress must be request-scoped. Phase 3 clones the baseline `http.Transport` for a context carrying a resolved proxy, leaving the global client unchanged.

## 2026-09-10 — direct media is not the entire media surface

The first Phase 3 pass covered `MetaConnector.Download`, but upstream normal Meta-to-Matrix conversion also reaches `mediadl.DownloadMedia()` through `ReuploadFileToMatrix()`. Avatar callbacks independently call `DownloadAvatar()`. Both paths can therefore perform Meta/CDN-bound traffic without traversing the direct-media handler.

Implication: traffic-class claims must be traced from all consumers of a shared network helper, not inferred from one obvious route. Phase 3 injects account-aware media context in `FBMessageEvent.ConvertMessage`, in the direct-media handler, and in account-bound avatar downloads. A green direct-media test alone is insufficient evidence of media isolation.

## 2026-09-11 — resolver clients must not inherit generic HTTP behavior

The first hardened Phase 3 resolver client disabled ambient proxy environment variables and added a timeout, but still inherited Go's default redirect behavior and accepted an unbounded response body. An internal redirect is not part of the resolver contract and should not be allowed to move a bearer-authenticated request to another destination.

Implication: security-sensitive internal clients need an explicit redirect policy, response-size bound and strict response contract. Phase 3 now rejects redirects without following them, caps resolver bodies at 64 KiB and validates returned proxy scheme/authority/path/query/fragment.

## 2026-09-11 — integration fixtures can hide identity bootstrap defects

The initial Phase 3 topology fixture pre-populated both `meta_account_id` and `mautrix_login_id` with `123`. That made established resolver calls look correct but skipped the real lifecycle in which the provider identity may be known before BridgeV2 has persisted its login ID.

The repository uniqueness constraints prevent duplicate non-null identities, but the previous resolver lookup incorrectly treated a still-`NULL` counterpart as a conflict when mautrix later supplied both identifiers. A second audit pass also established that identity ownership must be checked globally before active-status filtering: a login ID owned by a disabled connection must still conflict with another connection's provider identity rather than being treated as free.

Implication: integration fixtures must model identity state transitions, not only the steady state. Resolver lookup now accepts an additional identifier when its stored column is still null only if the other identifier uniquely selects one record; multiple matches or contradictory persisted values fail closed even when one record is inactive. Only after unique identity ownership is established is active tenant/connection state considered. The topology deliberately prebinds only `meta_account_id` and later resolves with both IDs.

## 2026-09-11 — first-login transport must explicitly transition traffic class before persistence

After cookie validation, upstream reuses the same Messagix client for the first MQTT connection. The first Phase 3 patch left that client's resolver callback configured as traffic class `login` and called `connectWithTable` directly, bypassing the normal established-connect helper.

This did not change the assigned proxy because Phase 2 keeps one sticky assignment per connection, but it made the traffic-class contract inaccurate and could break future class-specific policy or observability. An initial attempted fix performed the transition after `BridgeV2.NewLogin`; further audit showed that a resolver failure there could return a login error after the login had already been persisted or cached.

Implication: after deriving the deterministic `UserLogin.ID`, successful cookie login must switch the Messagix client to the account/login-aware `messaging` resolver **before** `NewLogin` persists BridgeV2 state and before the first MQTT connection. A resolver failure then aborts without partial login persistence. Tests observe the resolver sequence `login` then `messaging`.

## 2026-09-11 — dynamic Instagram identity is not equivalent to Facebook identity

Facebook/Messenger cookie identity (`c_user`) and the later FBID-derived BridgeV2 login ID align on the pinned baseline. Instagram starts from `ds_user_id`, while upstream may later derive a different FBID for `UserLogin.ID`. Treating those as interchangeable without an explicit identity transition can create false conflicts or incorrect routing.

Implication: dynamic egress is restricted to Facebook/Messenger until the control-plane model explicitly represents the Instagram identity transition. Documentation and staging claims must not imply Instagram support merely because upstream exposes an Instagram login flow.

## 2026-09-11 — arbitrary environment secret references create a confused-deputy path

The first environment-backed secret provider accepted any `env:VARIABLE` reference. That allowed an egress profile to name unrelated process credentials such as `CONTROL_PLANE_INTERNAL_TOKEN`. If such a profile also pointed at an operator-controlled proxy, the control plane could convert an internal service credential into outbound proxy authentication material.

Implication: persisted egress secret references must be constrained to a namespace that is explicitly provisioned for proxy credentials. The initial provider accepts only `env:EGRESS_PROXY_*`; arbitrary service/process environment variables are not dereferenceable through an egress profile. This restriction is enforced both by management validation and by the secret provider itself.

## 2026-09-11 — request-scoped media isolation has a resource tradeoff

Cloning an `http.Transport` per scoped media request avoids shared mutable proxy state and is correct for tenant isolation. Chunked video, however, can issue multiple requests and therefore create multiple transient transports whose idle connections are not explicitly closed by the current helper. The pinned upstream avatar download path also reads the response body without an explicit `Close`, which is additional lifecycle debt even though it is not a tenant-routing defect.

Implication: this is not a known cross-tenant leak, but it is a performance/resource risk under heavy media load. A future optimization should reuse one scoped client for a complete logical download or explicitly manage response/idle-connection lifecycle without restoring process-global proxy mutation.

## 2026-09-11 — source reconstruction is reproducible, container bytes are not yet hermetic

The mautrix source commit and Go module graph are pinned, but the stack Dockerfile still uses tagged base images and distribution package repositories rather than immutable image digests and package snapshots. GitHub Actions are also referenced by version tags rather than immutable action commit SHAs.

Implication: describe the current guarantee as deterministic source reconstruction/build recipe, not bit-for-bit reproducible container output. Stronger supply-chain reproducibility would pin base-image digests, GitHub Action SHAs and native package inputs and use least-privilege workflow permissions.

## Updating this record

Add a discovery when CI, upstream inspection, deployment behavior or a real integration boundary disproves an assumption or establishes a reusable operational constraint. Avoid using this document as a substitute for changing a normative contract when behavior or architecture actually changes.
