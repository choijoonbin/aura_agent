from __future__ import annotations

from typing import Any

from .governed_domain_core import GovernedPayloadCodec


class EvaluationDatasetMissing(RuntimeError):
    pass


class EvaluationDatasetNotApproved(RuntimeError):
    pass


def require_pii_approved_dataset(
    connection: Any,
    codec: GovernedPayloadCodec,
    *,
    tenant_id: int,
    dataset_id: str,
    lock: bool = False,
) -> tuple[dict[str, object], int]:
    fence = " FOR SHARE" if lock else ""
    row = connection.execute(
        """SELECT resource_version, snapshot_envelope
             FROM ai_admin_control_resources
            WHERE tenant_id = %s AND resource_type = 'EVALUATION_DATASET'
              AND resource_id = %s"""
        + fence,
        (tenant_id, dataset_id),
    ).fetchone()
    if row is None:
        raise EvaluationDatasetMissing("The evaluation dataset is unavailable.")
    dataset = codec.decrypt_json(
        row["snapshot_envelope"],
        tenant_id=tenant_id,
        resource_type="admin-control-resource",
        resource_id=f"EVALUATION_DATASET:{dataset_id}",
        field="snapshot",
    )
    if dataset.get("piiState") != "PASS":
        raise EvaluationDatasetNotApproved(
            "Evaluation execution requires a dataset with a completed PII PASS decision."
        )
    return dataset, int(row["resource_version"])
