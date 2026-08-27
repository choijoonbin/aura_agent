class RunInProgress(RuntimeError):
    pass


class RequestIdConflict(RuntimeError):
    pass


class RunStoreUnavailable(RuntimeError):
    pass
