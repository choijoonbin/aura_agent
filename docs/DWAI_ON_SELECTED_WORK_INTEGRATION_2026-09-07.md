# Selected Work AI Assistance: Integration Evidence

Date: 2026-09-07 (Asia/Seoul)

Status: code and scoped regression complete; external Azure model connectivity blocked.
No production GO claim, permission widening, partial commit, or push is made.

## Resumed Scope

Close the Work-to-DWAI selected-source integration and approval-expert conversation
continuation. Preserve Work ownership, shared observability changes, existing
envelope encryption and completed lease visibility. This is not a claim that all
historical DWAI roadmap items or the entire three-repository release are complete.

## Implemented Contract

- The browser sends the question plus a typed `AskSelectedWork` binding: source
  system, canonical UUID, expected version and approval-task obligation key.
  It does not serialize the work inbox, titles, excerpts or full documents into
  the question. Questions never enter navigation URLs.
- The Agent requires signed APP.ASK and source-app permissions, the original
  authenticated cookie/Bearer, and the Gateway-signed auth session family.
- Original-session `/api/auth/me?permissionPrefix=APP.ASK,<source-app>` rechecks
  tenant, user, TENANT plane, session family, grants and explicit denies.
  Public owner requests carry `X-Tenant-ID`, not spoofable internal identity
  headers. Service tokens, role and permission assertions are not forwarded.
- Exactly one owner record is read through Gateway. UUID, version, obligation
  and classification are checked. The collector never falls back to a whole
  collection on failure.
- Metadata evidence explicitly excludes unseen original documents, attachments
  and policies. Payload bodies and unrelated timeline/participant data are not
  included. Restricted or suspicious evidence is blocked.
- Session/source authorization and version are checked again after generation
  and when replaying a completed request. Failure masks the answer/citations;
  model usage already incurred is not hidden.
- Cancellation and source/agent navigation reject late UI results. Only a
  COMPLETED response with a known agent and valid conversation UUID can produce
  a Work-to-DWAI continuation link.
- Encrypted assistant messages now retain `agentKey` and `selectedWork`.
  Conversation GET optionally verifies `agentKey`. Runtime follow-up rebinds to
  the persisted selection before reading sources or calling the model, and
  rejects agent/source drift. No new database table or migration was needed.
- Approval deep links preserve their conversation ID. Missing, forbidden or
  mismatched conversations display an error without a follow-up composer.
- Ask remains read-only: no approval, submission or automatic business action.

## Owner Routes

| Source | Required source app | Exact owner GET through Gateway |
| --- | --- | --- |
| PERSONAL_TASK | APP.WORK | `/api/platform/v1/workspace/work-hub/personal-tasks/{id}` |
| SERVICE_REQUEST | APP.EMPLOYEE_SERVICES | `/api/platform/v1/services/requests/{id}` |
| APPROVAL_TASK | APP.APPROVALS | `/api/approvals/v1/tasks/{id}` |
| APPROVAL_REQUEST | APP.APPROVALS | `/api/approvals/v1/requests/{id}/detail` |

WORKSPACE-native rows currently have no exact owner detail route. The client
rejects that source and the Agent returns SELECTED_WORK_UNSUPPORTED. A collection
search is deliberately not used as a substitute for exact authorization.

## Real Local Runtime Evidence

Command: `.venv/bin/python scripts/verify_selected_work_runtime.py`

The script uses an existing local smoke account through real Gateway authentication
and an owned, non-sensitive disposable personal task. No permission is added.

| Observation | Final result |
| --- | --- |
| Login, explicit permission read, original-session recheck | HTTP 200 |
| Exact personal task creation/read | HTTP 200 |
| Stream stages | AUTHORIZING, RETRIEVING, REASONING, VERIFYING, PERSISTING, COMPLETED |
| Answer outcome | COMPLETED / ANSWER_GROUNDED_FALLBACK / one citation |
| Actual answer provider | DWP_GROUNDED_FALLBACK, not Azure generation |
| Conversation GET | HTTP 200, two messages, stored answer equals returned answer |
| Follow-up without client selected context | Same conversation ID, persisted selection restored |
| Source after both questions | Unchanged |
| Stale expected version | SELECTED_WORK_STALE, ABSTAINED, model NOT_INVOKED |
| Automatic business execution | False |
| Disposable task cleanup | HTTP 200 |
| Final-run conversation cleanup | HTTP 204, HTTP 204 |
| Logout | HTTP 200 |

Three real integration mismatches were repaired: missing SERVICE_GATEWAY_URL in
the local devctl defaults; `/me` omitting permissions unless explicitly requested;
and Gateway requiring the public X-Tenant-ID header. Auth/PEP checks were not
relaxed. The Agent was already restarted by the coordinating task; this task
did not issue a duplicate restart.

An independent synthetic model probe using the actual configured provider failed
with ModelCallFailed -> httpx.ConnectError, classified as DNS/name-resolution
failure. No provider HTTP response was received. Credentials, endpoint and raw
response bodies were not printed. This is an environment blocker, not evidence
of successful Azure model invocation. DNS/network access and the configured
endpoint must be verified by the environment owner, then the real smoke rerun.

Earlier diagnostic conversations from the initial read-only checks remain subject
to normal conversation retention; no broad user-conversation deletion was made.
The final verifier cleans up only conversation IDs it creates in that invocation.

## Verification

| Gate | Result |
| --- | --- |
| Full Agent pytest, fresh PostgreSQL scratch | 443 passed, 1 dedicated-upgrade test skipped |
| Dedicated V17-to-current upgrade database test | 1 passed separately; no remaining unexecuted test from that run |
| Both scratch migration histories | 31 versions, 31 distinct versions |
| Both owned scratch databases after DROP | 0 remaining |
| compileall | PASS |
| Agent OpenAPI runtime snapshot and frontend generated sync | PASS |
| Frontend selected-work/conversation/runtime/model unit tests | 4 files, 32 passed |
| Node24 full nonincremental typecheck | PASS |
| Scoped ESLint | 0 errors, 0 warnings |
| Approval continuation desktop/mobile E2E | 10 passed |
| Existing studio/archive Chromium E2E | 16 passed |
| Production reachability | PASS: 1304/1307 reachable, 3 governed verification roots |
| Local devctl regression | 9 passed |
| Scoped diff check | PASS |

The 16-screen regression includes 320/390px, 200% text, dark and forced-colors
coverage. Desktop/mobile screenshots were visually inspected, with no horizontal
overflow or overlapping transcript/composer/rail found in the selected cases.

Screen evidence in the frontend repository:

- `test-results/dwaion-selected-work/dwaion-selected-conversati-96437-ied-conversation-and-source-chromium/approval-conversation.png`
- `test-results/dwaion-selected-work/dwaion-selected-conversati-96437-ied-conversation-and-source-mobile/approval-conversation.png`
- `test-results/dwaion-conversation-regression/` contains studio/archive reflow evidence.

API/E2E fixture tests exercise real application boundaries with controlled owner
and model fixtures. They are not represented as real external Azure execution.
Whole frontend release build is owned by the coordinating integration task;
this task does not substitute scoped tests for its final release Gate.

## Exact Owned Files

Agent modified:

- `src/dwp_agent/contracts.py`
- `src/dwp_agent/context_broker.py`
- `src/dwp_agent/ask_runtime.py`
- `src/dwp_agent/workspace_authorization.py`
- `src/dwp_agent/conversation_exchange.py`
- `src/dwp_agent/main.py` (conversation GET agent check only)
- `contracts/openapi/agent-public.json`

Agent new:

- `src/dwp_agent/grounded_context.py`
- `src/dwp_agent/selected_work_context.py`
- `src/dwp_agent/ask_response_guard.py`
- `src/dwp_agent/conversation_continuation.py`
- `tests/test_selected_work_context.py`
- `tests/test_selected_work_runtime.py`
- `tests/test_selected_work_api.py`
- `scripts/verify_selected_work_runtime.py`
- This evidence document.

Frontend:

- `apps/dwp/src/features/dwaion/dwaion-workspace.tsx`
- `libs/shared-utils/src/api/agent-selected-work-api.ts` and `.test.ts`
  (Work owner final supported-source/COMPLETED validation preserved)
- `libs/shared-utils/src/api/agent-conversation-api.ts` and `.test.ts`
- `e2e/dwaion-selected-conversation.spec.ts`
- `e2e/dwaion-conversation-design.spec.ts` (GET fixture query matching)
- `libs/api-contracts/openapi/agent-public.json`
- `libs/api-contracts/src/agent-public.ts`

Backend:

- `scripts/devctl.py` (explicit local Gateway address only)
- `scripts/tests/test_devctl.py` (default and override regression)

Work dialog/page changes and shared Agent observability/migrations remain owned
by their respective tasks. They are not claimed as this task's changes.

## Remaining Boundaries

1. Actual external Azure generation is not operationally verified until DNS and
   endpoint access are resolved. Never relabel fallback as external AI success.
2. New selected conversations preserve exact scope across follow-up. Legacy
   encrypted messages without agent metadata retain the legacy DWP_ASSISTANT
   default; approval identity is not invented or backfilled from URL text.
3. Historical authorized answers remain readable under conversation access and
   retention policy. Current source revocation prevents new answers/replay, not
   retroactive deletion of historical evidence. A different retention policy
   requires a separate explicit product decision.
4. Native WORKSPACE exact-detail support and full original-document grounding
   require owner-service contracts. They are not implied by metadata-only support.
