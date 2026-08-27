class GovernanceStoreUnavailable(RuntimeError):
    pass


class GovernancePolicyConflict(RuntimeError):
    pass


class GovernancePolicyNotInitialized(RuntimeError):
    pass
