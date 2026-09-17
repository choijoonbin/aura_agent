# DWAI.ON production gate

DWAI.ON user and administration routes are fail closed. A deployment is ready only when the following evidence is recorded for that environment.

## Authorization and storage

- Apply database migrations through `V41` before starting the API or workers.
- Enable product authorization v21, v22 and v24 only after the gateway and Agent owner PEP load the matching sealed checksums. V24 covers governed research downloads, routine webhook/evidence/rollback, and artifact comments.
- Configure the managed envelope key provider, privacy fingerprint secret, tenant identity signature verification, and the Agent PostgreSQL database.
- Keep provider support identities outside tenant SELF and CONFIG_SCOPE authority.

## Attachments and research

- The attachment broker must verify the provider-observed SHA-256, size, and MIME type. AV, DLP, parser, OCR when required, index, and citation stages must all pass before an attachment can become `READY`.
- Provider absence or a failed stage remains `PARTIAL`, `BLOCKED`, or `FAILED`. It must never be reported as ready.
- Deep Research requires approved source connectors, the context broker, an approved model route, and the research worker flag. A run becomes `COMPLETED` only with a citation-backed result and persisted receipt.
- PDF, signed audit export, branch/merge, sensitivity recalculation, and cache fallback stay disabled until their governed providers are installed. Capability responses include the recovery path.

## Routines and artifact collaboration

- Routine activation and the fenced scheduler are implemented. Enable the routine worker only after its lease, idempotency, budget, notification, retry, and compensation policies are configured. A missing downstream dispatcher yields a partial or blocked receipt.
- Team artifact collaboration requires the ACL broker for external membership and source-access decisions. Local versioning, conflict records, audit history, preflight, shares, expiry, revoke, and access requests remain tenant and resource scoped.
- Automatic masking, synthetic replacement, and provider notification actions stay disabled until the corresponding broker capability is attested.

## Administration and deletion

- Control-plane mutations use maker-checker separation, optimistic target versions, immutable preflight evidence, command-bound idempotency, and a trusted worker observation. Only a domain receipt plus a versioned result snapshot can produce `SUCCEEDED` or `ROLLED_BACK`.
- Emergency recovery requires the independent step-up header and a payload acknowledgement.
- Outcome aggregates must enforce the configured privacy threshold and return no fabricated cost, latency, or impact measurements.
- Personal-data deletion retry rechecks legal holds, requeues failed targets only, preserves completed disposition receipts, and records a new immutable retry command, audit event, and outbox intent.

Run the OpenAPI snapshot check, architecture gate, authorization generator `--check`, and the applicable PostgreSQL integration suites before enabling any production capability flag.
