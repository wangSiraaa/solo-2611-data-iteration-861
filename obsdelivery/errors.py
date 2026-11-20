"""Exception types shared by the semantics service and the delivery library."""

from __future__ import annotations

from typing import Iterable, Mapping, Any


class ValidationError(ValueError):
    """A request is malformed or violates a contract."""


class BatchRejected(Exception):
    """An observation admission batch was rejected atomically."""

    def __init__(self, reasons: Iterable[Mapping[str, Any]]):
        self.reasons = list(reasons)
        super().__init__("observation batch rejected: " + repr(self.reasons))


class ReferenceConflict(Exception):
    """A reference version already exists with different immutable content."""


class ReportConflict(Exception):
    """A report id already exists with different immutable content."""


class ReferenceNotFound(KeyError):
    pass


class ReportNotFound(KeyError):
    pass


class ProductConflict(Exception):
    """A product id already exists with different immutable content."""


class ProductNotFound(KeyError):
    pass


class DeliveryNotFound(KeyError):
    pass


class DeliveryIdConflict(Exception):
    """A successful delivery id is being reused with a different request."""


class DeliveryRejected(Exception):
    """No legal delivery exists for at least one requested record.

    A rejected task publishes no delivery rows.  ``attempt_id`` identifies the
    audit entry (if one was recorded), not a successful delivery identity.
    """

    def __init__(
        self,
        delivery_id: str,
        reasons: Iterable[Mapping[str, Any]],
        attempt_id: int | None = None,
    ):
        self.delivery_id = delivery_id
        self.reasons = list(reasons)
        self.attempt_id = attempt_id
        super().__init__(
            f"delivery {delivery_id!r} rejected: " + repr(self.reasons)
        )


class CalibrationNotFound(KeyError):
    pass


class CalibrationConflict(Exception):
    """A segment or calibration product id exists with different content."""


class CalibrationRejected(Exception):
    """Pulse constraints are inconsistent, so no calibration product is written.

    ``feasible_slope`` and ``witness`` describe the (possibly empty) feasible
    slope interval with exact boundary evidence; ``attempt_id`` identifies the
    audit entry, not a successful product identity.
    """

    def __init__(
        self,
        product_id: str,
        reasons: Iterable[Mapping[str, Any]],
        attempt_id: int | None = None,
    ):
        self.product_id = product_id
        self.reasons = list(reasons)
        self.attempt_id = attempt_id
        super().__init__(
            f"calibration product {product_id!r} rejected: " + repr(self.reasons)
        )


class PairRequestRejected(Exception):
    """A paired-delivery request is malformed or its calibration is inconsistent."""

    def __init__(self, package_id: str, reasons: Iterable[Mapping[str, Any]]):
        self.package_id = package_id
        self.reasons = list(reasons)
        super().__init__(
            f"paired delivery {package_id!r} rejected: " + repr(self.reasons)
        )


class PairDeliveryNotSatisfied(Exception):
    """The request is well-formed but no admissible calibration meets the bar.

    Carries the exact overlap extremes so the caller can check the verdict:
    ``max_overlap < min_common`` proves NONE.  Nothing is published.
    """

    def __init__(self, package_id: str, evidence: Mapping[str, Any], attempt_id: int | None = None):
        self.package_id = package_id
        self.evidence = dict(evidence)
        self.attempt_id = attempt_id
        super().__init__(
            f"paired delivery {package_id!r} not satisfied: "
            f"max_overlap={evidence.get('max_overlap')!r} "
            f"< min_common={evidence.get('threshold')!r}"
        )


class PairIdConflict(Exception):
    """A published pair id is being reused with a different request."""


class PairNotFound(KeyError):
    pass


class IntegrityError(Exception):
    """Stored immutable content failed independent read-back verification."""
