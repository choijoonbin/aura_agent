from __future__ import annotations

from enum import StrEnum

from psycopg import Error as DatabaseError

from .governance_store import GovernanceStoreUnavailable
from .key_provider import KeyProviderConfigurationError, normalized_environment
from .operational_gate_catalog import OPERATIONAL_GATE_CATALOG
from .operational_gate_contracts import (
    GateEnvironment,
    GateStatus,
    OperationalGateKey,
)
from .operational_gate_store import PostgresOperationalGateStore
from .operational_gate_store_provider import get_operational_gate_store


class DeliveryCapability(StrEnum):
    ASK = "ASK"
    ACTION = "ACTION"


class OperationalDeliveryConfigurationError(RuntimeError):
    pass


class OperationalDeliveryNotReady(RuntimeError):
    def __init__(
        self,
        *,
        capability: DeliveryCapability,
        environment: GateEnvironment,
        blocking_gates: tuple[OperationalGateKey, ...],
    ) -> None:
        self.capability = capability
        self.environment = environment
        self.blocking_gates = blocking_gates
        super().__init__(
            f"DWAI-ON {capability.value.lower()} is not approved for "
            f"{environment.value.lower()}."
        )


_DELIVERY_CRITICAL_GATES = frozenset(
    definition.gate_key
    for definition in OPERATIONAL_GATE_CATALOG
    if definition.delivery_critical
)
_ASK_GATES = _DELIVERY_CRITICAL_GATES - {OperationalGateKey.ACTION_APPROVAL}
_ACTION_GATES = _DELIVERY_CRITICAL_GATES


def gate_environment() -> GateEnvironment | None:
    try:
        environment = normalized_environment()
    except KeyProviderConfigurationError as error:
        raise OperationalDeliveryConfigurationError(
            "DWP_ENVIRONMENT must resolve to local, development, staging, or production."
        ) from error
    if environment == "local":
        return None
    if environment == "dev":
        return GateEnvironment.DEVELOPMENT
    if environment == "qa":
        return GateEnvironment.STAGING
    if environment == "prod":
        return GateEnvironment.PRODUCTION
    raise OperationalDeliveryConfigurationError(
        "DWP_ENVIRONMENT must resolve to local, development, staging, or production."
    )


def validate_delivery_gate_runtime(
    store: PostgresOperationalGateStore | None = None,
) -> None:
    if gate_environment() is None:
        return
    selected = store or get_operational_gate_store()
    if not selected.schema_ready():
        raise OperationalDeliveryConfigurationError(
            "The DWAI-ON operational gate schema is unavailable."
        )


def require_delivery_capability(
    *,
    tenant_id: str,
    capability: DeliveryCapability,
    actor_user_id: str | None = None,
    store: PostgresOperationalGateStore | None = None,
) -> None:
    environment = gate_environment()
    if environment is None:
        return
    try:
        required = _ASK_GATES if capability == DeliveryCapability.ASK else _ACTION_GATES
        states = (store or get_operational_gate_store()).runtime_gate_states(
            tenant_id=tenant_id,
            environment=environment,
            gate_keys=required,
        )
    except (DatabaseError, GovernanceStoreUnavailable, ValueError) as error:
        raise OperationalDeliveryConfigurationError(
            "DWAI-ON operational delivery state is unavailable."
        ) from error

    blocking = tuple(
        sorted(
            (
                gate_key
                for gate_key in required
                if states.get(gate_key) != GateStatus.APPROVED
            ),
            key=lambda gate_key: gate_key.value,
        )
    )
    if blocking:
        raise OperationalDeliveryNotReady(
            capability=capability,
            environment=environment,
            blocking_gates=blocking,
        )
