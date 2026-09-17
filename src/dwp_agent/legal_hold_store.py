from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from .governed_domain_contracts import DomainKey, UpsertRetentionPolicyRequest
from .personal_domain_security import PersonalDomainIdentity


class LegalHoldCommands:
    def _sync_legal_hold(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        domain: DomainKey,
        request: UpsertRetentionPolicyRequest,
    ) -> None:
        current = connection.execute(
            """SELECT hold_id FROM ai_personal_data_legal_holds
                WHERE tenant_id = %s AND domain_key = %s
                  AND hold_state = 'ACTIVE' FOR UPDATE""",
            (identity.tenant_id, domain.value),
        ).fetchone()
        directive = request.legal_hold_directive
        if not request.legal_hold:
            if current is None:
                return
            connection.execute(
                """UPDATE ai_personal_data_legal_holds
                      SET hold_state = 'RELEASED', released_at = CURRENT_TIMESTAMP,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE hold_id = %s""",
                (current["hold_id"],),
            )
            self._legal_hold_event(
                connection,
                current["hold_id"],
                identity,
                domain,
                request.command_id,
                "RELEASED",
            )
            return
        if directive is None:
            return
        if current is None:
            hold_id = uuid4()
            connection.execute(
                """INSERT INTO ai_personal_data_legal_holds (
                       hold_id, tenant_id, domain_key, authority_reference,
                       dpo_subject_id, reason_code, effective_at, expires_at,
                       created_by_user_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    hold_id,
                    identity.tenant_id,
                    domain.value,
                    directive.authority_reference,
                    directive.dpo_subject_id,
                    directive.reason_code,
                    directive.effective_at,
                    directive.expires_at,
                    identity.user_id,
                ),
            )
            event_type = "CREATED"
        else:
            hold_id = current["hold_id"]
            connection.execute(
                """UPDATE ai_personal_data_legal_holds
                      SET authority_reference = %s, dpo_subject_id = %s,
                          reason_code = %s, effective_at = %s, expires_at = %s,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE hold_id = %s""",
                (
                    directive.authority_reference,
                    directive.dpo_subject_id,
                    directive.reason_code,
                    directive.effective_at,
                    directive.expires_at,
                    hold_id,
                ),
            )
            event_type = "UPDATED"
        self._legal_hold_event(
            connection,
            hold_id,
            identity,
            domain,
            request.command_id,
            event_type,
        )

    @staticmethod
    def _legal_hold_event(
        connection: Any,
        hold_id: UUID,
        identity: PersonalDomainIdentity,
        domain: DomainKey,
        command_id: UUID,
        event_type: str,
    ) -> None:
        connection.execute(
            """INSERT INTO ai_personal_data_legal_hold_events (
                   event_id, hold_id, tenant_id, domain_key, actor_user_id,
                   command_id, event_type, current_state)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                uuid4(),
                hold_id,
                identity.tenant_id,
                domain.value,
                identity.user_id,
                command_id,
                event_type,
                "RELEASED" if event_type == "RELEASED" else "ACTIVE",
            ),
        )

