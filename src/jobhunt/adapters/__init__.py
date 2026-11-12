"""Adapter package — importing it registers every built-in adapter."""

from .base import Adapter, get_adapter, register, registered_ats  # noqa: F401

# Import for side effect: each module registers its adapter class.
from . import greenhouse, lever, ashby, workday, amazon  # noqa: F401,E402
from . import (  # noqa: F401,E402
    apple, avature, balyasny, bamboohr, deel, digitalrecruiters, eightfold, epic,
    gem, ibm, icims, jazzhr, jobvite, njoyn, oraclehcm, phenom, pinpoint, radancy,
    recruitee, rentec, rippling, smartrecruiters, successfactors, successfactors_sitemap,
    simplify, workable,
)

__all__ = ["Adapter", "get_adapter", "register", "registered_ats"]
