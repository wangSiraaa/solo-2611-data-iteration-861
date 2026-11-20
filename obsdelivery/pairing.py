"""Paired, same-shot delivery under segment-calibration constraints.

A request names two exposures, each anchored to a published calibration
product, plus a required minimum common exposure:

* ``start_tick`` and ``duration`` are exact rationals; ``duration`` is station
  external time, so an exposure covers ``[start, start + duration]``.
* ``min_common`` is the least overlap the two exposures must share.

Both exposures may be anchored to the *same* calibration product (one station,
one segment).  Then they must share one calibration, so the start difference
is ``a*(tick2 - tick1)`` and the intercept cancels.  If they are anchored to
two different products, the two calibrations are chosen independently.

The verdict is one of:

* ``ALL``     — every admissible calibration meets ``min_common``;
* ``PARTIAL`` — some admissible calibration meets it, some do not;
* ``NONE``    — no admissible calibration meets it.

Every verdict is persisted as an immutable row with the exact overlap extremes
and a checkable witness.  ``NONE`` publishes no pair row (only an audit
attempt) and raises :class:`PairDeliveryNotSatisfied` carrying the same
evidence; the two stations' designs are deterministic functions of the stored
products, so a re-submission reproduces the verdict.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from .calibration import CalibrationService
from .calibration_algo import (
    Feasibility,
    Pulse,
    classify_independent,
    classify_shared,
    independent_witness,
    shared_witness,
)
from .canonical import canonical_json, digest, frac_str, parse_fraction
from .errors import (
    CalibrationNotFound,
    IntegrityError,
    PairDeliveryNotSatisfied,
    PairIdConflict,
    PairNotFound,
    PairRequestRejected,
    ValidationError,
)
from .storage import Store, utcnow

ALL = "ALL"
PARTIAL = "PARTIAL"
NONE = "NONE"


def _require_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string, got {value!r}")
    return value


def _positive_fraction(value: Any, field: str) -> Fraction:
    try:
        parsed = parse_fraction(value, field=field)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if parsed <= 0:
        raise ValidationError(f"{field} must be strictly positive")
    return parsed


def _exposure(obj: Any, label: str) -> dict:
    if not isinstance(obj, dict):
        raise ValidationError(f"{label} must be an object")
    product_id = _require_id(obj.get("product_id"), f"{label}.product_id")
    try:
        start = parse_fraction(obj.get("start_tick"), field=f"{label}.start_tick")
        duration = _positive_fraction(obj.get("duration"), f"{label}.duration")
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return {
        "product_id": product_id,
        "start_tick": start,
        "duration": duration,
    }


class PairDeliveryService:
    """Classify and persist paired same-shot deliveries."""

    def __init__(self, store: Store, calibration: CalibrationService | None = None):
        self.store = store
        self.calibration = calibration or CalibrationService(store)

    # -- public API ---------------------------------------------------------
    def evaluate(self, request: dict) -> dict:
        """Classify a request without publishing anything."""
        normalised = self._normalise_request(request)
        return self._evaluate(normalised)

    def submit(self, pair_id: str, request: dict) -> dict:
        """Publish (or replay) a paired delivery, refusing NONE."""
        _require_id(pair_id, "pair_id")
        normalised = self._normalise_request(request)
        evaluation = self._evaluate(normalised)
        request_digest = digest(normalised["canonical"])

        existing = self.store.conn.execute(
            "SELECT * FROM paired_deliveries WHERE pair_id=?", (pair_id,)
        ).fetchone()
        if existing is not None:
            if existing["request_digest"] != request_digest:
                raise PairIdConflict(
                    f"pair {pair_id!r} exists for a different request"
                )
            return self._view(existing, created=False)

        classification = evaluation["classification"]
        if classification == NONE:
            attempt_id = self._record_attempt(
                pair_id, request_digest, "rejected", classification, evaluation
            )
            raise PairDeliveryNotSatisfied(pair_id, evaluation, attempt_id=attempt_id)

        content_digest = digest(
            {
                "kind": "paired_delivery",
                "pair_id": pair_id,
                "request": normalised["canonical"],
                "evaluation": evaluation,
            }
        )
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO paired_deliveries "
                "(pair_id, mode, classification, product_1, product_2, request_json, "
                " request_digest, evidence_json, content_digest, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pair_id,
                    evaluation["mode"],
                    classification,
                    normalised["exposure_1"]["product_id"],
                    normalised["exposure_2"]["product_id"],
                    canonical_json(normalised["canonical"]),
                    request_digest,
                    canonical_json(evaluation),
                    content_digest,
                    utcnow(),
                ),
            )
            conn.execute(
                "INSERT INTO paired_delivery_attempts "
                "(pair_id, request_digest, status, classification, reasons_json, "
                " evidence_json, created_at) "
                "VALUES (?, ?, 'published', ?, ?, ?, ?)",
                (
                    pair_id,
                    request_digest,
                    classification,
                    canonical_json([]),
                    canonical_json(evaluation),
                    utcnow(),
                ),
            )
        row = self.store.conn.execute(
            "SELECT * FROM paired_deliveries WHERE pair_id=?", (pair_id,)
        ).fetchone()
        return self._view(row, created=True)

    def get_pair(self, pair_id: str) -> dict:
        row = self.store.conn.execute(
            "SELECT * FROM paired_deliveries WHERE pair_id=?", (pair_id,)
        ).fetchone()
        if row is None:
            raise PairNotFound(pair_id)
        return self._view(row, created=False)

    #: Alias matching the delivery-library naming.
    get_delivery = get_pair

    def attempts(self, pair_id: str) -> list[dict]:
        rows = self.store.conn.execute(
            "SELECT attempt_id, pair_id, request_digest, status, classification, "
            "reasons_json, evidence_json, created_at FROM paired_delivery_attempts "
            "WHERE pair_id=? ORDER BY attempt_id",
            (pair_id,),
        ).fetchall()
        return [
            {
                "attempt_id": row["attempt_id"],
                "pair_id": row["pair_id"],
                "request_digest": row["request_digest"],
                "status": row["status"],
                "classification": row["classification"],
                "reasons": _loads(row["reasons_json"]),
                "evidence": _loads(row["evidence_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # -- internals ----------------------------------------------------------
    def _normalise_request(self, request: dict) -> dict:
        if not isinstance(request, dict):
            raise ValidationError("request must be an object")
        exposure_1 = _exposure(request.get("exposure_1"), "exposure_1")
        exposure_2 = _exposure(request.get("exposure_2"), "exposure_2")
        try:
            min_common = _positive_fraction(request.get("min_common"), "min_common")
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        canonical = {
            "exposure_1": {
                "product_id": exposure_1["product_id"],
                "start_tick": exposure_1["start_tick"],
                "duration": exposure_1["duration"],
            },
            "exposure_2": {
                "product_id": exposure_2["product_id"],
                "start_tick": exposure_2["start_tick"],
                "duration": exposure_2["duration"],
            },
            "min_common": min_common,
        }
        return {
            "exposure_1": exposure_1,
            "exposure_2": exposure_2,
            "min_common": min_common,
            "canonical": canonical,
        }

    def _load_product(self, product_id: str) -> dict:
        try:
            return self.calibration.get_calibration_product(product_id)
        except CalibrationNotFound:
            raise CalibrationNotFound(product_id)

    @staticmethod
    def _same_calibration(a: dict, b: dict) -> bool:
        """Two products encode the same admissible calibration band and pulses."""
        return (
            a["slope"] == b["slope"]
            and a["feasible_slope"] == b["feasible_slope"]
            and a["pulses"] == b["pulses"]
        )

    @staticmethod
    def _product_pulses(product: dict) -> list[Pulse]:
        return [
            Pulse(
                pulse_id=p["pulse_id"],
                tick=p["tick"],
                lo=Fraction(p["lo"]),
                hi=Fraction(p["hi"]),
                revision=p["revision"],
                bits=p["bits"],
            )
            for p in product["pulses"]
        ]

    def _feasibility(self, product: dict) -> Feasibility:
        lo = Fraction(product["slope"]["lo"])
        hi = Fraction(product["slope"]["hi"])
        feasible = product["feasible_slope"]
        return Feasibility(
            consistent=True,
            requested_lo=lo,
            requested_hi=hi,
            feasible_lo=Fraction(feasible["lo"]),
            feasible_hi=Fraction(feasible["hi"]),
            witness_a=Fraction(product["witness"]["a"]),
            witness_b=Fraction(product["witness"]["b"]),
            reasons=(),
        )

    def _evaluate(self, normalised: dict) -> dict:
        exposure_1 = normalised["exposure_1"]
        exposure_2 = normalised["exposure_2"]
        threshold = normalised["min_common"]

        product_1 = self._load_product(exposure_1["product_id"])
        product_2 = self._load_product(exposure_2["product_id"])
        pulses_1 = self._product_pulses(product_1)
        pulses_2 = self._product_pulses(product_2)
        feas_1 = self._feasibility(product_1)
        feas_2 = self._feasibility(product_2)

        shared = product_1["segment_id"] == product_2["segment_id"]
        f1 = _int_tick(exposure_1["start_tick"])
        f2 = _int_tick(exposure_2["start_tick"])

        if shared and product_1["product_id"] != product_2["product_id"]:
            # Same segment must use ONE calibration.  Two different products
            # of the same segment are only admissible if they agree exactly;
            # anything else is ambiguous and must not be silently merged.
            if not self._same_calibration(product_1, product_2):
                raise PairRequestRejected(
                    normalised["exposure_1"]["product_id"],
                    [
                        {
                            "reason": "same_segment_calibration_mismatch",
                            "segment_id": product_1["segment_id"],
                            "product_1": product_1["product_id"],
                            "product_2": product_2["product_id"],
                        }
                    ],
                )

        if shared:
            base = classify_shared(
                feas_1, pulses_1, f1, exposure_1["duration"],
                f2, exposure_2["duration"], threshold,
            )
            witness = shared_witness(
                feas_1, pulses_1, f1, exposure_1["duration"],
                f2, exposure_2["duration"], threshold,
            )
        else:
            base = classify_independent(
                feas_1, pulses_1, f1, exposure_1["duration"],
                feas_2, pulses_2, f2, exposure_2["duration"], threshold,
            )
            witness = independent_witness(
                feas_1, pulses_1, f1, exposure_1["duration"],
                feas_2, pulses_2, f2, exposure_2["duration"], threshold,
            )

        base["witness"] = {**base.get("witness", {}), "instance": witness}
        base["request"] = normalised["canonical"]
        base["product_1"] = {
            "product_id": product_1["product_id"],
            "content_digest": product_1["content_digest"],
            "segment_id": product_1["segment_id"],
            "station_id": product_1["station_id"],
        }
        base["product_2"] = {
            "product_id": product_2["product_id"],
            "content_digest": product_2["content_digest"],
            "segment_id": product_2["segment_id"],
            "station_id": product_2["station_id"],
        }
        base["shared_calibration"] = shared
        return _jsonable(base)

    def _record_attempt(
        self,
        pair_id: str,
        request_digest: str,
        status: str,
        classification: str,
        evaluation: dict,
    ) -> int:
        with self.store.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO paired_delivery_attempts "
                "(pair_id, request_digest, status, classification, reasons_json, "
                " evidence_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    pair_id,
                    request_digest,
                    status,
                    classification,
                    canonical_json(evaluation.get("reasons", [])),
                    canonical_json(evaluation),
                    utcnow(),
                ),
            )
            return cur.lastrowid

    def _view(self, row, created: bool) -> dict:
        evidence = _loads(row["evidence_json"])
        expected = digest(
            {
                "kind": "paired_delivery",
                "pair_id": row["pair_id"],
                "request": _loads(row["request_json"]),
                "evaluation": evidence,
            }
        )
        if expected != row["content_digest"]:
            raise IntegrityError(
                f"paired delivery {row['pair_id']!r} digest mismatch "
                f"({expected} != {row['content_digest']})"
            )
        return {
            "pair_id": row["pair_id"],
            "mode": row["mode"],
            "classification": row["classification"],
            "product_1": row["product_1"],
            "product_2": row["product_2"],
            "request": _loads(row["request_json"]),
            "request_digest": row["request_digest"],
            "evidence": evidence,
            "content_digest": row["content_digest"],
            "created": created,
        }


def _int_tick(value: Fraction) -> int:
    if value.denominator != 1:
        raise ValidationError(f"start_tick must be an integer, got {frac_str(value)}")
    return value.numerator


def _loads(text: str):
    import json

    return json.loads(text)


def _jsonable(value):
    """Recursively turn Fractions into exact rational strings for storage/output."""
    if isinstance(value, Fraction):
        return frac_str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value
