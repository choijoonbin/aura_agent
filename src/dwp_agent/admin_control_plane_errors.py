class AdminControlPlaneConflict(RuntimeError):
    pass


class AdminControlPlaneNotFound(RuntimeError):
    pass


class AdminControlPlaneDenied(RuntimeError):
    pass


class AdminControlPlaneUnavailable(RuntimeError):
    pass
