from __future__ import annotations

from dataclasses import dataclass

from .operational_gate_contracts import (
    GateCategory,
    GateEvidenceType,
    OperationalGateKey,
)


@dataclass(frozen=True)
class OperationalGateDefinition:
    gate_key: OperationalGateKey
    category: GateCategory
    external_owner: str
    delivery_critical: bool
    options: tuple[str, ...]
    recommended_option: str
    required_evidence_types: tuple[GateEvidenceType, ...]


OPERATIONAL_GATE_CATALOG = (
    OperationalGateDefinition(
        OperationalGateKey.MODEL_CREDENTIALS,
        GateCategory.AI_RUNTIME,
        "CLOUD_SECURITY",
        True,
        ("MANAGED_IDENTITY", "SECRET_REFERENCE"),
        "MANAGED_IDENTITY",
        (GateEvidenceType.CONFIGURATION_REFERENCE, GateEvidenceType.SECURITY_REVIEW),
    ),
    OperationalGateDefinition(
        OperationalGateKey.MODEL_LIFECYCLE_CAPACITY,
        GateCategory.AI_RUNTIME,
        "AI_PLATFORM",
        True,
        ("PINNED_GA", "CONTROLLED_AUTO_UPGRADE", "PROVISIONED_MANUAL"),
        "PINNED_GA",
        (GateEvidenceType.CONFIGURATION_REFERENCE, GateEvidenceType.TEST_RESULT),
    ),
    OperationalGateDefinition(
        OperationalGateKey.NETWORK_ISOLATION,
        GateCategory.CONNECTIVITY,
        "CLOUD_SECURITY",
        True,
        ("PRIVATE_ENDPOINT", "RESTRICTED_PUBLIC", "DEVELOPMENT_PUBLIC_ONLY"),
        "PRIVATE_ENDPOINT",
        (GateEvidenceType.CONFIGURATION_REFERENCE, GateEvidenceType.SECURITY_REVIEW),
    ),
    OperationalGateDefinition(
        OperationalGateKey.DATA_PROCESSING_LOCATION,
        GateCategory.CONNECTIVITY,
        "DATA_PROTECTION",
        True,
        ("REGIONAL", "DATA_ZONE", "GLOBAL_APPROVED"),
        "REGIONAL",
        (GateEvidenceType.CONFIGURATION_REFERENCE, GateEvidenceType.LEGAL_APPROVAL),
    ),
    OperationalGateDefinition(
        OperationalGateKey.SOURCE_CONNECTORS,
        GateCategory.ACCESS_CONTROL,
        "SOURCE_OWNER",
        True,
        ("SOURCE_NATIVE", "INDEXED_CONNECTOR", "HYBRID"),
        "SOURCE_NATIVE",
        (GateEvidenceType.CONFIGURATION_REFERENCE, GateEvidenceType.TEST_RESULT),
    ),
    OperationalGateDefinition(
        OperationalGateKey.SOURCE_ACL,
        GateCategory.ACCESS_CONTROL,
        "IDENTITY_SECURITY",
        True,
        ("QUERY_TIME", "INDEXED_ACL_SYNC", "HYBRID"),
        "QUERY_TIME",
        (GateEvidenceType.TEST_RESULT, GateEvidenceType.SECURITY_REVIEW),
    ),
    OperationalGateDefinition(
        OperationalGateKey.DATA_CLASSIFICATION_DLP,
        GateCategory.ACCESS_CONTROL,
        "DATA_PROTECTION",
        True,
        ("STRICT", "STANDARD", "CUSTOM"),
        "STRICT",
        (GateEvidenceType.CONFIGURATION_REFERENCE, GateEvidenceType.SECURITY_REVIEW),
    ),
    OperationalGateDefinition(
        OperationalGateKey.EVALUATION_DATASET,
        GateCategory.ASSURANCE,
        "AI_ASSURANCE",
        True,
        ("CUSTOMER_APPROVED", "ANONYMIZED_PRODUCTION", "SYNTHETIC_DEVELOPMENT_ONLY"),
        "CUSTOMER_APPROVED",
        (GateEvidenceType.TEST_RESULT, GateEvidenceType.BUSINESS_APPROVAL),
    ),
    OperationalGateDefinition(
        OperationalGateKey.RELEASE_APPROVAL,
        GateCategory.ASSURANCE,
        "AI_ASSURANCE",
        True,
        ("MAKER_CHECKER", "CHANGE_BOARD", "AUTOMATED_WITH_EXCEPTION"),
        "MAKER_CHECKER",
        (GateEvidenceType.TEST_RESULT, GateEvidenceType.BUSINESS_APPROVAL),
    ),
    OperationalGateDefinition(
        OperationalGateKey.ACTION_APPROVAL,
        GateCategory.ASSURANCE,
        "BUSINESS_OWNER",
        True,
        ("USER_CONFIRMATION", "SEPARATE_APPROVAL", "BLOCKED"),
        "SEPARATE_APPROVAL",
        (GateEvidenceType.CONFIGURATION_REFERENCE, GateEvidenceType.BUSINESS_APPROVAL),
    ),
    OperationalGateDefinition(
        OperationalGateKey.TENANT_KMS,
        GateCategory.DATA_PROTECTION,
        "CLOUD_SECURITY",
        True,
        ("CUSTOMER_MANAGED_KEY", "DWP_MANAGED_PER_TENANT", "LOCAL_DEVELOPMENT_KEY"),
        "CUSTOMER_MANAGED_KEY",
        (GateEvidenceType.CONFIGURATION_REFERENCE, GateEvidenceType.TEST_RESULT),
    ),
    OperationalGateDefinition(
        OperationalGateKey.RETENTION_LEGAL_HOLD,
        GateCategory.DATA_PROTECTION,
        "RECORDS_LEGAL",
        True,
        ("CUSTOMER_SCHEDULE", "REGULATORY_PROFILE", "DWP_DEVELOPMENT_DEFAULT"),
        "CUSTOMER_SCHEDULE",
        (GateEvidenceType.LEGAL_APPROVAL, GateEvidenceType.TEST_RESULT),
    ),
    OperationalGateDefinition(
        OperationalGateKey.AUDIT_RESILIENCE,
        GateCategory.OPERATIONS,
        "SERVICE_OPERATIONS",
        True,
        ("CUSTOMER_SIEM", "DWP_OBSERVABILITY", "HYBRID"),
        "HYBRID",
        (GateEvidenceType.CONFIGURATION_REFERENCE, GateEvidenceType.RUNBOOK),
    ),
)


_CATALOG_BY_KEY = {definition.gate_key: definition for definition in OPERATIONAL_GATE_CATALOG}


def operational_gate_definition(gate_key: OperationalGateKey) -> OperationalGateDefinition:
    return _CATALOG_BY_KEY[gate_key]
