class OperationalGateConflict(RuntimeError):
    pass


class OperationalGateInvalidTransition(RuntimeError):
    pass


class OperationalGateMissingEvidence(OperationalGateInvalidTransition):
    def __init__(self, missing_evidence_types: list[str]) -> None:
        self.missing_evidence_types = tuple(missing_evidence_types)
        super().__init__(
            "Required evidence is missing: " + ", ".join(self.missing_evidence_types)
        )


class OperationalGateSeparationOfDutyViolation(RuntimeError):
    def __init__(self, conflicting_role: str) -> None:
        self.conflicting_role = conflicting_role
        super().__init__(
            "The gate owner, configurator, or validator cannot approve the same "
            "delivery decision."
        )
