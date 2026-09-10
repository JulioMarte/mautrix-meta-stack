# Meta Control Plane Chatwoot Tenancy and Routing Contract

Status: normative for `feature/meta-control-plane`

## Purpose

This document defines how control-plane tenants map into Chatwoot and prevents the implementation from treating a Chatwoot inbox ID as a sufficient security boundary.

## Initial tenancy model

The MVP MAY use one Chatwoot installation and one Chatwoot account/workspace containing multiple API inboxes, provided every binding is explicitly tenant-scoped in the control plane.

The control plane MUST NOT assume that numeric Chatwoot IDs are globally unique across installations/accounts. Every persistent Chatwoot reference therefore carries the relevant `api_base_url`/installation identity and `chatwoot_account_id` in addition to inbox/conversation IDs.

A future deployment may choose one Chatwoot account per tenant for stronger administrative isolation. That choice must not require changing the normalized messaging domain model.

## Required binding hierarchy

The authority chain is:

```text
tenant
-> chatwoot_binding
-> account/workspace
-> inbox
-> contact/source
-> conversation
```

A `meta_connection` may reference only a `chatwoot_binding` owned by the same tenant.

A `conversation_binding` must retain the Chatwoot account, inbox, source/contact and conversation identifiers necessary to address the same conversation deterministically after restart.

## API inbox model

The control plane uses Chatwoot API inbox semantics for bridged conversations. The adapter is responsible for creating/resolving the necessary contact/source/conversation objects and then creating messages in the correct conversation.

The implementation MUST make creation retry-safe. A transient timeout must not cause duplicate contacts or conversations on retry.

Where Chatwoot provides a caller-controlled stable source/contact identifier, the adapter SHOULD derive it deterministically from a tenant-scoped provider identity rather than a random value. The exact derivation must be documented before Phase 4 completion.

## Contact identity

Remote Meta participant identity must be scoped by at least `tenant_id + meta_connection_id + remote_contact_id`.

The same Facebook user contacting two different tenant accounts MUST NOT be automatically treated as the same CRM contact across those tenants unless a future explicit identity-linking policy authorizes it.

Display names are attributes, never identity keys.

## Conversation identity

A remote thread maps to one tenant-scoped Chatwoot conversation binding for the active mapping generation.

At minimum the uniqueness contract is equivalent to:

```text
(meta_connection_id, remote_thread_id) -> one active conversation_binding
```

A Chatwoot conversation ID by itself is never sufficient to infer tenant ownership.

## Webhook ingress

The webhook endpoint receives account-level Chatwoot events and MUST first determine the configured Chatwoot binding/account before resolving any conversation.

Only events from explicitly configured account/inbox surfaces are eligible for routing. Unknown account, inbox or conversation identifiers fail closed and produce observable diagnostics.

The exact authenticity mechanism depends on the deployed Chatwoot version/capabilities and remains a release-blocking contract for Phase 5. Network allowlisting alone is not sufficient authentication where a stronger supported mechanism exists.

## Agent reply eligibility

Only outgoing/agent messages intended for the bound API conversation are candidates for Meta delivery.

System notes, private notes, unsupported message types, webhook echoes and messages originally imported from Matrix/Meta MUST not be forwarded blindly.

The adapter must use explicit event/message metadata and persisted correlation, not text comparison, to distinguish provenance.

## Idempotent creation and ambiguous failures

Contact, source, conversation and message creation need deterministic retry behavior.

For each remote operation the adapter MUST either:

- use a stable downstream idempotency/correlation identifier supported by the API; or
- reconcile/search for the result after an ambiguous timeout before creating another object.

Blind retry of create operations is forbidden when it can create duplicate customer-visible records/messages.

## Tenant disabling

Disabling a tenant or Meta connection immediately makes its Chatwoot binding ineligible for new routing. Historical Chatwoot objects are not deleted automatically.

Re-enabling must reuse existing bindings unless an operator explicitly migrates to a different inbox/account.

## Chatwoot binding migration

Changing an active connection from inbox/account A to B is a privileged audited operation.

The implementation MUST define whether existing remote threads keep their historical conversation bindings or are explicitly migrated. Silent remapping of existing conversations is forbidden. For the MVP, the safer default is to preserve existing bindings and apply the new destination only to explicitly migrated or newly discovered conversations.

## Credential scope

Chatwoot API credentials belong to a `chatwoot_binding`/installation context, not directly to a conversation. Credentials must be stored as secret references and resolved at request time.

Logs and audit state may identify the binding/account/inbox but MUST NOT contain the API token.

## Required CI proofs

CI MUST prove:

- two tenants can use distinct inboxes without cross-routing;
- colliding-looking Chatwoot numeric IDs from different mocked installations/accounts do not cross-bind;
- the same remote contact ID in two tenants creates tenant-isolated contact/source state;
- duplicate/ambiguous create retries do not create duplicate conversation bindings;
- unknown account/inbox webhook events are rejected;
- incoming Meta-originated Chatwoot messages are not echoed back to Matrix;
- agent replies target the exact bound Matrix room;
- disabling a tenant/connection blocks new Chatwoot routing;
- changing an inbox does not silently rewrite historical conversation bindings.