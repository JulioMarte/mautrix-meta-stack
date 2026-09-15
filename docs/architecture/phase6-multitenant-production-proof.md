# Phase 6 Multi-Tenant Production Proof

Status: executable acceptance contract for Phase 6. This document defines the deterministic repository proof; it does not replace controlled staging evidence against real Meta/provider infrastructure.

## Purpose

Phase 6 closes the architecture by proving that two independent tenant connections can operate concurrently without sharing routing state, egress state or downstream conversation destinations, and that injected dependency failures remain fail-closed.

The proof must observe side effects at the relevant boundaries. A database row or returned `proxy_url` alone is not sufficient evidence that protected traffic used the intended egress.

## Test topology

The dedicated `Phase 6 Multi-tenant production proof` workflow runs two complementary jobs.

### Fork multi-account egress

The workflow reconstructs the fork from exact upstream commit `ed37c9e6ce47e83dc75b9abea7b636302715b9bc`, applies the Phase 3 egress delta and the Phase 6 test patch, then creates two independent account/login contexts.

Account A and account B resolve to separate controlled HTTP proxy servers. First-login and established/reconnect HTTP transports perform real requests through those proxies toward a direct-egress sentinel URL. Acceptance requires:

- resolver context A never selects B and context B never selects A;
- login transport A reaches only proxy A and login transport B reaches only proxy B;
- reconnect/messaging transport A reaches only proxy A and reconnect/messaging transport B reaches only proxy B;
- the direct sentinel receives zero protected requests.

Existing Phase 3 tests remain authoritative for the request-scoped media and E2EE traffic-class paths; Phase 6 adds concurrent account separation rather than replacing those tests.

### Control-plane multi-tenant topology

The container topology creates two tenants with intentionally distinct identities:

| Property | Tenant A | Tenant B |
| --- | --- | --- |
| Meta account | `meta-phase6-a` | `meta-phase6-b` |
| mautrix login | `login-phase6-a` | `login-phase6-b` |
| Matrix room | `!phase6-a:matrix.example.com` | `!phase6-b:matrix.example.com` |
| Chatwoot account/inbox | `1 / 10` | `2 / 20` |
| Controlled egress | `proxy-a` | `proxy-b` |
| Egress secret namespace | `EGRESS_PROXY_PHASE6_A_*` | `EGRESS_PROXY_PHASE6_B_*` |
| Webhook signing secret | A-specific | B-specific |

The topology interleaves requests for both tenants so process-global accidental state is observable.

## Required assertions

The gate is green only when it proves all of the following on the same candidate SHA:

1. All four resolver traffic classes (`login`, `messaging`, `media`, `e2ee`) retain one sticky assignment per connection.
2. Tenant A and B have distinct assignments and the controlled proxy endpoints observe four protected requests each.
3. The direct-egress sentinel observes zero requests, including during assigned-proxy failure.
4. Matrix-to-Chatwoot traffic creates distinct conversations/messages in the intended account/inbox and a duplicate Matrix event produces no second side effect.
5. Chatwoot-to-Matrix traffic sends each human reply to the exact persisted room for that tenant.
6. The same provider message ID may exist in two different Chatwoot bindings without cross-tenant deduplication because persisted Chatwoot source IDs are binding-namespaced.
7. A binding/account/inbox/conversation mismatch fails closed.
8. An unavailable assigned proxy fails at that proxy and does not fall back to the direct sentinel.
9. A protocol-level Chatwoot timeout marks the Matrix event retryable; retrying the exact event after recovery produces exactly one Chatwoot message.
10. Restart preserves connection identity, conversation binding, sticky egress resolution and processed-event state.
11. An exact canonical webhook replay after restart remains a duplicate.
12. Reusing a processed webhook ID with a changed canonical payload is a terminal identity conflict: it returns HTTP `409` with `EVENT_IDENTITY_CONFLICT` and produces no additional Matrix side effect. It is not classified as a retryable dependency failure.
13. Synthetic credential canaries do not appear in captured service logs; the workflow also masks generated canaries from normal Actions output.

## Fault-injection design

Faults are injected at protocol boundaries rather than by mutating application internals:

- **assigned proxy unavailable:** the selected proxy double returns failure while the direct sentinel remains available;
- **Chatwoot timeout:** the Chatwoot double delays one message-create request beyond the gateway timeout without stopping container health;
- **duplicate/reuse:** exact replay and same-ID/different-payload cases are tested independently; same-ID/different-payload is terminal because retrying cannot make an already persisted identity match a different canonical payload;
- **control-plane restart:** the application container restarts while the persistent SQLite volume and external doubles remain intact.

Phase 3 remains the source of resolver-down/unauthorized/malformed-response and mautrix reconnect-specific fault proofs. Phase 4/5 remain the source of their direction-specific attachment and ambiguous-side-effect proofs. Phase 6 composes these guarantees under concurrent tenant state.

## Evidence boundary

This deterministic gate does **not** claim that a real Facebook/Meta session was exercised in GitHub Actions, nor that a production provider's public exit IP was observed. Those checks require controlled staging with disposable provider accounts and production-like egress endpoints before promotion to `main`.

A Phase 6 candidate may be called complete only after this dedicated workflow and all inherited repository validation gates are green on the exact candidate SHA. It becomes integrated only after merge to `dev` and green post-merge validation on the resulting `dev` SHA.
