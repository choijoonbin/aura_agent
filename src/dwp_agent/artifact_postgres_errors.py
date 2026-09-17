from __future__ import annotations

from functools import wraps
from typing import Callable, TypeVar

from psycopg import Error as PsycopgError

from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
    GovernedDomainUnavailable,
)


T = TypeVar("T")


def translated_artifact_operation(function: Callable[..., T]) -> Callable[..., T]:
    @wraps(function)
    def wrapped(*args: object, **kwargs: object) -> T:
        try:
            return function(*args, **kwargs)
        except (GovernedDomainConflict, GovernedDomainNotFound, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise GovernedDomainUnavailable("Governed artifacts are unavailable.") from error

    return wrapped
