from __future__ import annotations

import base64
import hashlib
from typing import Any
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .canonical_json import canonical_json_bytes
from .deletion_job_queries import read_deletion_job
from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
    GovernedDomainUnavailable,
    advisory_lock,
    require_command_replay,
)
from .personal_data_evidence_contracts import (
    CreatePersonalDataEvidenceCommandRequest,
    PersonalDataEvidenceAction,
    PersonalDataEvidenceCommand,
    PersonalDataEvidenceCommandState,
)
from .personal_data_certificate_attestation import PersonalDataCertificateAttestationError, verify_certificate_attestation
from .personal_data_evidence_pdf import render_deletion_certificate
from .personal_data_evidence_provider import (
    PersonalDataEvidenceProvider,
    PersonalDataEvidenceProviderContext,
    PersonalDataEvidenceProviderError,
    build_personal_data_evidence_provider,
)
from .personal_data_evidence_provider_outcomes import (
    validate_personal_data_provider_outcome,
)
from .personal_data_evidence_store_support import (
    public_evidence_result,
    validated_evidence_parameters,
)
from .personal_domain_security import PersonalDomainIdentity
from .personal_data_evidence_download_store import PersonalDataEvidenceDownloadStoreMixin


class PersonalDataEvidenceStore(PersonalDataEvidenceDownloadStoreMixin):
    def __init__(self, retention_store: Any) -> None:
        self.database_url = retention_store.database_url
        self.codec = retention_store.codec
        self.fingerprints = retention_store.fingerprints

    def execute(
        self,
        identity: PersonalDomainIdentity,
        deletion_job_id: UUID,
        action: PersonalDataEvidenceAction,
        request: CreatePersonalDataEvidenceCommandRequest,
        *,
        provider: PersonalDataEvidenceProvider | None = None,
    ) -> PersonalDataEvidenceCommand:
        provider = provider or build_personal_data_evidence_provider(action)
        if not provider.configured:
            raise GovernedDomainUnavailable(
                "The requested personal-data evidence provider is not configured."
            )
        parameters = validated_evidence_parameters(action, request.parameters)
        payload = {
            "deletionJobId": str(deletion_job_id),
            "action": action.value,
            **request.model_dump(mode="json", by_alias=True),
            "parameters": parameters,
        }
        if len(canonical_json_bytes(payload)) > 16_384:
            raise GovernedDomainConflict("The evidence command payload is too large.")
        proof = self.fingerprints.command(
            tenant_id=identity.tenant_id,
            session_id=identity.auth_session_id,
            purpose="personal-data-evidence-command",
            payload=payload,
        )
        evidence: dict[str, object]
        evidence_digest: str
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                advisory_lock(
                    connection,
                    "personal-data-evidence-command",
                    identity.tenant_id,
                    identity.user_id,
                    request.command_id,
                )
                replay = self._command_row(
                    connection,
                    identity,
                    deletion_job_id=deletion_job_id,
                    command_id=request.command_id,
                )
                if replay is not None:
                    require_command_replay(
                        replay["session_fingerprint"],
                        replay["request_fingerprint"],
                        proof,
                    )
                    if replay["action"] != action.value:
                        raise GovernedDomainConflict(
                            "The command ID is already bound to another evidence action."
                        )
                    command = self._command(replay, identity.tenant_id)
                    if command.state != PersonalDataEvidenceCommandState.PENDING:
                        return command
                    evidence = self.codec.decrypt_json(
                        replay["request_envelope"],
                        tenant_id=identity.tenant_id,
                        resource_type="personal-data-evidence-command",
                        resource_id=str(request.command_id),
                        field="request",
                    )["evidence"]
                    if not isinstance(evidence, dict):
                        raise GovernedDomainUnavailable(
                            "Stored personal-data evidence is invalid."
                        )
                    evidence_digest = replay["evidence_digest"]
                else:
                    job = read_deletion_job(
                        connection,
                        deletion_job_id=deletion_job_id,
                        tenant_id=identity.tenant_id,
                        user_id=identity.user_id,
                        fingerprints=self.fingerprints,
                    )
                    if job.attempt_count != request.expected_revision:
                        raise GovernedDomainConflict(
                            "The deletion evidence revision has changed."
                        )
                    evidence = {
                        "schema": "dwp.personal-data.deletion-evidence.v1",
                        "deletionJob": job.model_dump(mode="json", by_alias=True),
                    }
                    evidence_digest = hashlib.sha256(
                        canonical_json_bytes(evidence)
                    ).hexdigest()
                    request_envelope = self.codec.encrypt_json(
                        {
                            "reasonCode": request.reason_code,
                            "changeReason": request.change_reason,
                            "parameters": parameters,
                            "evidence": evidence,
                        },
                        tenant_id=identity.tenant_id,
                        resource_type="personal-data-evidence-command",
                        resource_id=str(request.command_id),
                        field="request",
                    )
                    connection.execute(
                        """INSERT INTO ai_personal_data_evidence_commands (
                               command_id, deletion_job_id, tenant_id, user_id, action,
                               expected_revision, session_fingerprint, request_fingerprint,
                               evidence_digest, request_envelope, state)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'PENDING')""",
                        (
                            request.command_id,
                            deletion_job_id,
                            identity.tenant_id,
                            identity.user_id,
                            action.value,
                            request.expected_revision,
                            proof.session_fingerprint,
                            proof.request_fingerprint,
                            evidence_digest,
                            request_envelope,
                        ),
                    )
                    self._command_event(
                        connection,
                        identity,
                        deletion_job_id,
                        request.command_id,
                        action,
                        "REQUESTED",
                        None,
                        evidence_digest,
                    )
        except (GovernedDomainConflict, GovernedDomainNotFound, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise GovernedDomainUnavailable(
                "The personal-data evidence command could not be started."
            ) from error

        context = PersonalDataEvidenceProviderContext(
            command_id=request.command_id,
            deletion_job_id=deletion_job_id,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            correlation_id=identity.correlation_id,
            action=action,
            evidence_digest=evidence_digest,
            evidence=evidence,
            parameters=parameters,
        )
        try:
            provider_result = provider.execute(context)
            if (
                provider_result.command_id != context.command_id
                or provider_result.deletion_job_id != context.deletion_job_id
                or provider_result.action != context.action
                or provider_result.evidence_digest != context.evidence_digest
            ):
                raise PersonalDataEvidenceProviderError(
                    "PERSONAL_DATA_EVIDENCE_PROVIDER_BINDING_INVALID",
                    "Reject the receipt and repair deletion job, command, action and digest propagation.",
                )
            state = PersonalDataEvidenceCommandState(provider_result.state)
            provider_receipt_id = provider_result.provider_receipt_id
            safe_error_code = provider_result.safe_error_code
            recovery_hint = provider_result.recovery_hint
            result: dict[str, object] = dict(provider_result.result)
            if state == PersonalDataEvidenceCommandState.COMPLETED:
                try:
                    result = validate_personal_data_provider_outcome(
                        action,
                        provider_result.result,
                        evidence_digest=evidence_digest,
                        parameters=parameters,
                        evidence=evidence,
                    )
                except (TypeError, ValueError) as error:
                    raise PersonalDataEvidenceProviderError(
                        "PERSONAL_DATA_EVIDENCE_PROVIDER_RECEIPT_INVALID",
                        "Repair the provider outcome evidence and retry with the same command ID.",
                    ) from error
            if action == PersonalDataEvidenceAction.SIGNED_CERTIFICATE and state == PersonalDataEvidenceCommandState.COMPLETED:
                attestation = verify_certificate_attestation(context, provider_result)
                result.update(
                    {
                        "signature": provider_result.signature,
                        "signingKeyId": provider_result.signing_key_id,
                        "signingAlgorithm": provider_result.signing_algorithm,
                        "evidenceDigest": evidence_digest,
                        "signedPayloadSha256": attestation.signed_payload_sha256,
                        "signedPayloadBase64Url": attestation.signed_payload_base64url,
                        "signingKeyFingerprint": attestation.signing_key_fingerprint,
                    }
                )
                pdf = render_deletion_certificate(
                    {
                        "Deletion job": deletion_job_id,
                        "Command ID": request.command_id,
                        "Tenant": identity.tenant_id,
                        "User": identity.user_id,
                        "Manifest schema": "dwp.personal-data.deletion-certificate-signing.v1",
                        "Action": action.value,
                        "Evidence digest": evidence_digest,
                        "Provider receipt": provider_receipt_id,
                        "Signed payload SHA-256": attestation.signed_payload_sha256,
                        "Signed payload base64url": attestation.signed_payload_base64url,
                        "Signing key": provider_result.signing_key_id,
                        "Algorithm": provider_result.signing_algorithm,
                        "Key fingerprint": attestation.signing_key_fingerprint,
                        "Signature": provider_result.signature,
                    }
                )
                result["documentSha256"] = hashlib.sha256(pdf).hexdigest()
                result["_certificatePdfBase64"] = base64.b64encode(pdf).decode("ascii")
        except (PersonalDataEvidenceProviderError, PersonalDataCertificateAttestationError) as error:
            state = PersonalDataEvidenceCommandState.FAILED
            provider_receipt_id = None
            safe_error_code = error.code
            recovery_hint = error.recovery_hint
            result = {"providerAccepted": False}
        return self._finalize(
            identity,
            deletion_job_id=deletion_job_id,
            command_id=request.command_id,
            action=action,
            state=state,
            provider_receipt_id=provider_receipt_id,
            result=result,
            safe_error_code=safe_error_code,
            recovery_hint=recovery_hint,
        )

    def get(
        self,
        identity: PersonalDomainIdentity,
        deletion_job_id: UUID,
        command_id: UUID,
    ) -> PersonalDataEvidenceCommand:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                row = self._command_row(
                    connection,
                    identity,
                    deletion_job_id=deletion_job_id,
                    command_id=command_id,
                )
                if row is None:
                    raise GovernedDomainNotFound(
                        "The personal-data evidence command is unavailable."
                    )
                return self._command(row, identity.tenant_id)
        except GovernedDomainNotFound:
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise GovernedDomainUnavailable(
                "The personal-data evidence command is unavailable."
            ) from error

    def _finalize(
        self,
        identity: PersonalDomainIdentity,
        *,
        deletion_job_id: UUID,
        command_id: UUID,
        action: PersonalDataEvidenceAction,
        state: PersonalDataEvidenceCommandState,
        provider_receipt_id: str | None,
        result: dict[str, object],
        safe_error_code: str | None,
        recovery_hint: str | None,
    ) -> PersonalDataEvidenceCommand:
        receipt_id = uuid4()
        public_result = public_evidence_result(result)
        result_fingerprint = self.fingerprints.value(
            tenant_id=identity.tenant_id,
            purpose="personal-data-evidence-result",
            payload={
                "receiptId": str(receipt_id),
                "commandId": str(command_id),
                "action": action.value,
                "state": state.value,
                "providerReceiptId": provider_receipt_id,
                "result": public_result,
                "safeErrorCode": safe_error_code,
                "recoveryHint": recovery_hint,
            },
        )
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                advisory_lock(
                    connection,
                    "personal-data-evidence-command",
                    identity.tenant_id,
                    identity.user_id,
                    command_id,
                )
                current = self._command_row(
                    connection,
                    identity,
                    deletion_job_id=deletion_job_id,
                    command_id=command_id,
                )
                if current is None:
                    raise GovernedDomainNotFound(
                        "The personal-data evidence command is unavailable."
                    )
                if current["state"] != PersonalDataEvidenceCommandState.PENDING.value:
                    return self._command(current, identity.tenant_id)
                result_envelope = self.codec.encrypt_json(
                    result,
                    tenant_id=identity.tenant_id,
                    resource_type="personal-data-evidence-command",
                    resource_id=str(command_id),
                    field="result",
                )
                row = connection.execute(
                    """UPDATE ai_personal_data_evidence_commands
                          SET state = %s, receipt_id = %s, provider_receipt_id = %s,
                              result_envelope = %s, result_fingerprint = %s,
                              safe_error_code = %s, recovery_hint = %s,
                              completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                        WHERE command_id = %s AND tenant_id = %s AND user_id = %s
                          AND deletion_job_id = %s AND state = 'PENDING'
                    RETURNING *""",
                    (
                        state.value,
                        receipt_id,
                        provider_receipt_id,
                        result_envelope,
                        result_fingerprint,
                        safe_error_code,
                        recovery_hint,
                        command_id,
                        identity.tenant_id,
                        identity.user_id,
                        deletion_job_id,
                    ),
                ).fetchone()
                if row is None:
                    raise GovernedDomainConflict(
                        "The personal-data evidence command changed concurrently."
                    )
                self._command_event(
                    connection,
                    identity,
                    deletion_job_id,
                    command_id,
                    action,
                    state.value,
                    safe_error_code,
                    result_fingerprint,
                )
                return self._command(row, identity.tenant_id)
        except (GovernedDomainConflict, GovernedDomainNotFound, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise GovernedDomainUnavailable(
                "The personal-data evidence command could not be finalized."
            ) from error

    def _command_row(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        *,
        deletion_job_id: UUID,
        command_id: UUID,
    ) -> Any:
        return connection.execute(
            """SELECT * FROM ai_personal_data_evidence_commands
                WHERE command_id = %s AND deletion_job_id = %s
                  AND tenant_id = %s AND user_id = %s""",
            (command_id, deletion_job_id, identity.tenant_id, identity.user_id),
        ).fetchone()

    def _command(self, row: Any, tenant_id: int) -> PersonalDataEvidenceCommand:
        result: dict[str, object] | None = None
        if row["result_envelope"]:
            result = public_evidence_result(
                self.codec.decrypt_json(
                    row["result_envelope"],
                    tenant_id=tenant_id,
                    resource_type="personal-data-evidence-command",
                    resource_id=str(row["command_id"]),
                    field="result",
                )
            )
        return PersonalDataEvidenceCommand(
            command_id=row["command_id"],
            deletion_job_id=row["deletion_job_id"],
            action=row["action"],
            state=row["state"],
            expected_revision=row["expected_revision"],
            receipt_id=row["receipt_id"],
            provider_receipt_id=row["provider_receipt_id"],
            result_fingerprint=row["result_fingerprint"],
            result=result,
            safe_error_code=row["safe_error_code"],
            recovery_hint=row["recovery_hint"],
            download_available=(
                row["state"] == PersonalDataEvidenceCommandState.COMPLETED.value
                and row["action"] == PersonalDataEvidenceAction.SIGNED_CERTIFICATE.value
            ),
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )

    def _command_event(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        deletion_job_id: UUID,
        command_id: UUID,
        action: PersonalDataEvidenceAction,
        event_type: str,
        safe_error_code: str | None,
        evidence_fingerprint: str,
    ) -> None:
        connection.execute(
            """INSERT INTO ai_personal_data_evidence_command_events (
                   event_id, command_id, deletion_job_id, tenant_id, user_id,
                   actor_user_id, correlation_id, action, event_type,
                   safe_error_code, evidence_fingerprint)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                uuid4(),
                command_id,
                deletion_job_id,
                identity.tenant_id,
                identity.user_id,
                identity.user_id,
                identity.correlation_id,
                action.value,
                event_type,
                safe_error_code,
                evidence_fingerprint,
            ),
        )
