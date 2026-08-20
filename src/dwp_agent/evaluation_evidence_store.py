from __future__ import annotations

import csv
import io
from uuid import UUID

from psycopg import connect

from .governance_contracts import (
    EvaluationResult,
    EvaluationRun,
    EvaluationRunState,
    EvaluationRunSummary,
)


class EvaluationRunNotFound(RuntimeError):
    pass


class EvaluationEvidenceStoreMixin:
    database_url: str

    def list_runs(
        self, *, tenant_id: str, evaluation_set_id: UUID, limit: int = 20
    ) -> list[EvaluationRunSummary]:
        with connect(self.database_url) as connection:
            rows = connection.execute(
                """SELECT evaluation_run_id, evaluation_set_id, run_state,
                          case_count, passed_count, failed_count,
                          configuration_required_count, model_ref, created_at, completed_at
                     FROM ai_evaluation_runs
                    WHERE tenant_id = %s AND evaluation_set_id = %s
                    ORDER BY created_at DESC, evaluation_run_id DESC
                    LIMIT %s""",
                (int(tenant_id), evaluation_set_id, limit),
            ).fetchall()
        return [self._run_summary(row) for row in rows]

    def run_detail(
        self, *, tenant_id: str, evaluation_set_id: UUID, evaluation_run_id: UUID
    ) -> EvaluationRun:
        tenant = int(tenant_id)
        with connect(self.database_url) as connection:
            run_row = connection.execute(
                """SELECT evaluation_run_id, evaluation_set_id, run_state,
                          case_count, passed_count, failed_count,
                          configuration_required_count, model_ref, created_at, completed_at
                     FROM ai_evaluation_runs
                    WHERE tenant_id = %s AND evaluation_set_id = %s
                      AND evaluation_run_id = %s""",
                (tenant, evaluation_set_id, evaluation_run_id),
            ).fetchone()
            if run_row is None:
                raise EvaluationRunNotFound("The evaluation run was not found.")
            result_rows = connection.execute(
                """SELECT result.evaluation_case_id, evaluation_case.name,
                          result.outcome, result.status_code, result.grounded,
                          result.expected_terms_matched, result.expected_terms_total,
                          result.latency_ms
                     FROM ai_evaluation_results result
                     JOIN ai_evaluation_cases evaluation_case
                       ON evaluation_case.tenant_id = result.tenant_id
                      AND evaluation_case.evaluation_case_id = result.evaluation_case_id
                    WHERE result.tenant_id = %s AND result.evaluation_run_id = %s
                    ORDER BY result.created_at, result.evaluation_result_id""",
                (tenant, evaluation_run_id),
            ).fetchall()
        summary = self._run_summary(run_row)
        return EvaluationRun(
            evaluation_run_id=summary.evaluation_run_id,
            evaluation_set_id=summary.evaluation_set_id,
            run_state=summary.run_state,
            case_count=summary.case_count,
            passed_count=summary.passed_count,
            failed_count=summary.failed_count,
            configuration_required_count=summary.configuration_required_count,
            model_ref=summary.model_ref,
            results=[self._result(row) for row in result_rows],
            created_at=summary.created_at,
            completed_at=summary.completed_at,
        )

    def run_csv(
        self, *, tenant_id: str, evaluation_set_id: UUID, evaluation_run_id: UUID
    ) -> str:
        run = self.run_detail(
            tenant_id=tenant_id,
            evaluation_set_id=evaluation_set_id,
            evaluation_run_id=evaluation_run_id,
        )
        output = io.StringIO(newline="")
        output.write("\ufeff")
        writer = csv.writer(output)
        writer.writerow([
            "evaluationRunId", "evaluationSetId", "runState", "caseName", "outcome",
            "statusCode", "grounded", "expectedTermsMatched", "expectedTermsTotal",
            "latencyMs", "modelRef", "createdAt", "completedAt",
        ])
        for result in run.results:
            writer.writerow([
                str(run.evaluation_run_id),
                str(run.evaluation_set_id),
                run.run_state.value,
                self._safe_csv_cell(result.case_name),
                result.outcome.value,
                self._safe_csv_cell(result.status_code),
                str(result.grounded).lower(),
                result.expected_terms_matched,
                result.expected_terms_total,
                result.latency_ms,
                self._safe_csv_cell(run.model_ref or ""),
                run.created_at.isoformat(),
                run.completed_at.isoformat() if run.completed_at else "",
            ])
        return output.getvalue()

    @staticmethod
    def _run_summary(row) -> EvaluationRunSummary:
        pass_rate = (
            round(row[4] * 100 / row[3])
            if row[3]
            and row[2]
            in (
                EvaluationRunState.COMPLETED.value,
                EvaluationRunState.CONFIGURATION_REQUIRED.value,
            )
            else None
        )
        return EvaluationRunSummary(
            evaluation_run_id=row[0],
            evaluation_set_id=row[1],
            run_state=row[2],
            case_count=row[3],
            passed_count=row[4],
            failed_count=row[5],
            configuration_required_count=row[6],
            pass_rate=pass_rate,
            model_ref=row[7],
            created_at=row[8],
            completed_at=row[9],
        )

    @staticmethod
    def _result(row) -> EvaluationResult:
        return EvaluationResult(
            evaluation_case_id=row[0],
            case_name=row[1],
            outcome=row[2],
            status_code=row[3],
            grounded=row[4],
            expected_terms_matched=row[5],
            expected_terms_total=row[6],
            latency_ms=row[7],
        )

    @staticmethod
    def _safe_csv_cell(value: str) -> str:
        normalized = value.lstrip(" \t\r\n")
        return f"'{value}" if normalized.startswith(("=", "+", "-", "@")) else value
