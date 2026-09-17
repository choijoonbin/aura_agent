# S19 AI runtime control

This component governs the tenant `ASK_RUNTIME` model boundary. It does not govern voice STT/TTS,
meeting intelligence, or meeting-media providers, and its emergency stop must not be presented as a
tenant-wide AI stop. It does not store model credentials, infer provider prices, or treat a budget
alert as an execution stop.

## Runtime contract

Before retrieval, the runtime requires a tenant policy and checks the emergency stop, exact
provider/model allow-list, requested knowledge-source allow-list, and evaluation-gate state. Before
the model call it creates a tenant-scoped token reservation under a locked policy version. A hard
limit rejects the call; alert-only mode emits warnings and allows it. A successful provider response
settles locally observed input/output/total tokens. Each Ask lease generation receives a distinct
reservation, and the same generation cannot be admitted twice.
When a route declares a region, the configured `DWP_MODEL_REGION` must match it. A route without a
region does not claim a provider processing location.

`ALERT_ONLY` and `ENFORCED` are deliberately different states. `ALERT_ONLY` never claims to stop
execution. `ENFORCED` reserves a conservative UTF-8 byte ceiling for the complete model input plus
the configured maximum output tokens before invocation and blocks when that reservation would
exceed the period limit. Since a provider token cannot contain less than one source byte, the byte
count is a conservative admission ceiling without depending on an external tokenizer. Actual
provider input/output totals are stored after the response.

Controlled calls disable the gateway's automatic provider retry so one external request maps to one
reservation. A timeout, connection reset, non-success response, invalid response, or successful
response without complete usage telemetry keeps the full reservation charged as unresolved until a
future reconciliation process resolves it. Expired active reservations also remain charged; expiry
is an operational age marker, not permission to discard possible usage. A rejected 2xx response
with complete usage is settled before the rejection is propagated.
Only a failure proven to occur before provider dispatch, such as invalid local provider
configuration, releases its reservation without measurement.

The current Ask runtime does not execute model-selected tools. The API reports
`toolEnforcementState=NOT_CONNECTED`. `allowedToolKeys` is the tenant contract for a future governed
tool executor and is available through `AIRuntimeControl.authorize_tool`;
workplace-action previews remain governed by their existing action policy. No tool execution is
claimed until an executor calls that boundary.

This foundation also does not select a fallback model, impose a tenant concurrency limit, or change
the provider timeout. The allow-list applies to the one model route configured for Ask. Those
controls require separate execution adapters before the UI may show them as enforced.

## Administration API

The Agent routes are reached from the browser only through the existing `/api/agent/**` gateway
proxy, which injects the service and delegated identity headers.

| Browser proxy route | Method | Existing tenant authority |
|---|---|---|
| `/api/agent/v1/admin/ai-control` | GET | `ADMIN.DWAION_SAFETY:VIEW` or `:MANAGE` |
| `/api/agent/v1/admin/ai-control/bootstrap` | POST | `ADMIN.DWAION_SAFETY:UPDATE` or `:MANAGE` |
| `/api/agent/v1/admin/ai-control/policy` | PUT | `ADMIN.DWAION_SAFETY:UPDATE` or `:MANAGE` |
| `/api/agent/v1/admin/ai-control/emergency` | POST | `ADMIN.DWAION_SAFETY:MANAGE` |

The legacy broad `ADMIN.DWAION:MANAGE` authority is not accepted. A dedicated emergency-stop
authority is a future least-privilege hardening option; it is not required by this release because
the authorization registry is owned by another change stream.

Emergency disable blocks new Ask retrieval/model admissions and is checked again by the locked token
reservation immediately before invocation. It does not block voice or meeting AI paths and does not
claim to cancel a provider request that was already sent; in-flight cancellation needs
provider-specific support.

All commands use tenant headers, optimistic policy versions, append-only governance events, and an
explicit reason. Bootstrap is idempotent. Policy text rejects obvious credential material. Model
routes store identifiers and optional observation metadata only, never API keys or secret values.
Administrative configuration may set evaluation state only to `NOT_REQUIRED` or `PENDING`, with no
evidence timestamp or version. `PASSED`, `FAILED`, and `STALE` require a future trusted evaluator
receipt path. Until that exists, required evaluation gates fail closed with evidence state
`UNAVAILABLE`; a stored label alone cannot enable execution.

## Staged activation

`DWP_AI_RUNTIME_CONTROL_ENFORCEMENT_ENABLED` defaults to `false`. While false, existing Ask traffic
uses the prior runtime and does not require the new database policy, while the administration API
can be used to bootstrap and inspect policies. Enablement uses this order:

1. Deploy migration V36 and the application/API contracts with enforcement disabled.
2. Activate canonical product authorization v21 and verify the gateway emits its trusted route,
   scope, identity-plane, and decision-revision headers for all four routes.
3. Set `DWP_AGENT_PRODUCT_AUTHORIZATION_V21_ENABLED=true` so the Agent PEP owns and verifies those
   exact v21 routes.
4. Bootstrap every target tenant through the governed administration API and verify its model
   route, knowledge sources, token limit, evaluation gate, and emergency state in the overview.
5. Set `DWP_AI_RUNTIME_CONTROL_ENFORCEMENT_ENABLED=true` only for the prepared environment and run an
   Ask smoke test for every enabled tenant.

When the flag is true, missing policy or control storage denies Ask execution; there is no implicit
fallback. The overview reports `enforcementActivationState` and `controlScope=ASK_RUNTIME` so an
operator can distinguish configured policy from active enforcement.

## Measurement truth

`measured*Tokens` and `measurementFreshness` describe locally observed model responses for the
current UTC calendar month. `reservedTokens` includes active and unresolved reservations, while
`unmeasuredReservedTokens` identifies expired-active or `MEASUREMENT_MISSING` capacity held for
reconciliation. `estimatedCostMinor` and
`billedCostMinor` remain null, and provider usage, pricing, and billing states remain `UNAVAILABLE`,
until verified provider adapters supply those independent facts. Administrative policy requests
cannot label external model availability as verified.

The migration creates tenant-keyed policy, usage-period, reservation, and measurement tables.
Reservation and measurement lookups include tenant ID, run ID, and Ask lease generation, and the
runtime fails closed when policy or measurement storage is unavailable. Database migration V36,
canonical gateway product authorization, generated Agent bindings, and generated OpenAPI snapshots
must be deployed together with the application code.
