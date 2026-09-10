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

## Updating this record

Add a discovery when CI, upstream inspection, deployment behavior or a real integration boundary disproves an assumption or establishes a reusable operational constraint. Avoid using this document as a substitute for changing a normative contract when behavior or architecture actually changes.
