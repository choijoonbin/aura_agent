class DwaionWorkflowUnavailable(RuntimeError):
    pass


class DwaionWorkflowConflict(RuntimeError):
    pass


class DwaionWorkflowNotFound(RuntimeError):
    pass


class DwaionWorkflowForbidden(RuntimeError):
    pass
