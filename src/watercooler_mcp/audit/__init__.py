"""Audit primitives for Move 6 of the security consolidation.

Exports the egress-inventory scanner used by the CI gate
(:mod:`tests.integration.test_egress_inventory`) and the hygiene
helpers used by :mod:`tests.integration.test_class_p_permissions`.
"""

from .egress_inventory import (
    CLASS_P_ANNOTATION,
    AllowlistEntry,
    EgressClass,
    EgressSite,
    classify_site,
    evaluate_inventory,
    parse_allowlist,
    scan_module,
    scan_package,
)

__all__ = [
    "CLASS_P_ANNOTATION",
    "AllowlistEntry",
    "EgressClass",
    "EgressSite",
    "classify_site",
    "evaluate_inventory",
    "parse_allowlist",
    "scan_module",
    "scan_package",
]
