# DSH Second-Host Thin Adapter (`integrations/dsh/`)

Zero-dependency Node.js ESM thin adapter connecting DSH / Cordis to the shared **RPE-Harness C4 HostAPI** (`rpe host ...`), without duplicating execution, verification, or SQLite ledgers.

## G2 Security Boundaries
- **Model-callable tools (`modelTools`)**: `rpe_capabilities`, `rpe_start_task`, `rpe_show_task`. DSH injects trusted `host_id: "dsh"`, `owner`, `session_id`, `turn_id`, and `tool_call_id`; model arguments cannot override host identity.
- **Host-only user actions (`trustedActions`)**: `confirmTask`, `cancelTask`, `reconcileTask`, `handoffTask`. Never registered as model tools; ordinary tool permission never substitutes for `proposal digest` confirmation.
- **Cross-host handoff**: Creates a new `operation_id` and `session_id` with explicit source re-authorization and path remapping while invalidating the prior host's `confirmation_id`.

## Run End-to-End Tests
```sh
node --test integrations/dsh/test.mjs
```
