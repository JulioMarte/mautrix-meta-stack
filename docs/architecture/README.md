# Architecture documents

This directory contains active architecture and implementation contracts for the current repository line, plus historical material retained for reference.

For the current single-client product direction, start with:

- `managed-meta-onboarding.md` — customer-facing Meta/Facebook onboarding through the existing NiceGUI `/admin` surface, without Element in the normal workflow.
- `development-branch-workflow.md` — branch roles and promotion discipline.
- `production-readiness-ledger.md` — evidence and remaining production gates where applicable.

The repository also contains substantial multi-tenant/control-plane design material from the earlier architecture. The current deployment line intentionally uses one stack per customer, as described in the top-level `README.md`. Historical control-plane documents remain useful as security and implementation reference, but they are not prerequisites for the single-client onboarding path unless explicitly referenced by an active document.
