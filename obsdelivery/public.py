"""Public artefact rendering and verification.

* ``public_semantic_product`` — immutable canonical observation product.
* ``public_delivery`` — standalone delivery document with enough information
  for an independent reader to re-check the actual decimal representation
  without consulting the current observation state.

Both builders re-verify the stored content digests before rendering.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

from .calibration import CalibrationService
from .delivery import DeliveryService
from .pairing import PairDeliveryService
from .service import SemanticsService


def public_calibration_product(calibration: CalibrationService, product_id: str) -> dict:
    """Standalone calibration document an independent reader can re-check.

    Includes the exact pulses, the feasible slope band, an ``(a, b)`` witness
    and the moment associations.  ``verify_public_calibration`` re-derives all
    of these from the document alone.
    """
    product = calibration.get_calibration_product(product_id)
    return {
        "kind": "calibration_product",
        "product_id": product["product_id"],
        "segment_id": product["segment_id"],
        "station_id": product["station_id"],
        "slope": product["slope"],
        "feasible_slope": product["feasible_slope"],
        "witness": product["witness"],
        "pulses": product["pulses"],
        "ticks": product["ticks"],
        "moments": product["moments"],
        "content_digest": product["content_digest"],
    }


def verify_public_calibration(document: dict) -> list[str]:
    """Re-derive pulse consistency, the slope band and the witness exactly."""
    from .calibration_algo import Pulse, evaluate_calibration, mapped_moments

    problems: list[str] = []
    pulses = [
        Pulse(
            pulse_id=p["pulse_id"],
            tick=p["tick"],
            lo=Fraction(p["lo"]),
            hi=Fraction(p["hi"]),
            revision=p.get("revision", 0),
            bits=p.get("bits", ""),
        )
        for p in document["pulses"]
    ]
    lo = Fraction(document["slope"]["lo"])
    hi = Fraction(document["slope"]["hi"])
    feas = evaluate_calibration(lo, hi, pulses)
    if not feas.consistent:
        problems.append("pulses are inconsistent; no admissible calibration")
        return problems
    if Fraction(document["feasible_slope"]["lo"]) != feas.feasible_lo:
        problems.append("feasible lower slope disagrees with the pulses")
    if Fraction(document["feasible_slope"]["hi"]) != feas.feasible_hi:
        problems.append("feasible upper slope disagrees with the pulses")
    a = Fraction(document["witness"]["a"])
    b = Fraction(document["witness"]["b"])
    if not (feas.feasible_lo <= a <= feas.feasible_hi):
        problems.append("witness slope is outside the feasible band")
    for pulse in pulses:
        at = a * pulse.tick + b
        if not (pulse.lo <= at <= pulse.hi):
            problems.append(f"witness violates pulse {pulse.pulse_id!r}")
    if "moments" in document and document["moments"] is not None:
        ticks = document.get("ticks", [])
        if mapped_moments(feas, pulses, ticks) != document["moments"]:
            problems.append("stored moment associations disagree with the pulses")
    return problems


def public_paired_delivery(pairing: PairDeliveryService, pair_id: str) -> dict:
    """Standalone paired-delivery document (verdict + exact overlap evidence)."""
    view = pairing.get_pair(pair_id)
    return {
        "kind": "paired_delivery",
        "pair_id": view["pair_id"],
        "mode": view["mode"],
        "classification": view["classification"],
        "shared_calibration": view["evidence"]["shared_calibration"],
        "product_1": view["product_1"],
        "product_2": view["product_2"],
        "request": view["request"],
        "evidence": view["evidence"],
        "content_digest": view["content_digest"],
    }


def verify_public_pair(document: dict) -> list[str]:
    """Independently re-check a paired-delivery verdict's internal consistency.

    The verdict must agree with the overlap extremes it records: ``ALL`` iff
    ``min_overlap >= min_common``, ``NONE`` iff ``max_overlap < min_common``,
    ``PARTIAL`` otherwise; and a ``NONE`` verdict may not be published.
    """
    problems: list[str] = []
    threshold = Fraction(document["request"]["min_common"])
    evidence = document["evidence"]
    classification = document["classification"]
    if evidence["threshold"] != document["request"]["min_common"]:
        problems.append("evidence threshold disagrees with the request")
    lo = evidence.get("min_overlap")
    hi = evidence.get("max_overlap")
    if classification == "NONE":
        problems.append("a NONE verdict must not be published as a pair")
    if lo is not None and hi is not None:
        lo_f, hi_f = Fraction(lo), Fraction(hi)
        if not lo_f <= hi_f:
            problems.append("min overlap exceeds max overlap")
        expected = (
            "ALL" if lo_f >= threshold else ("NONE" if hi_f < threshold else "PARTIAL")
        )
        if expected != classification:
            problems.append(
                f"classification {classification} disagrees with overlap extremes "
                f"[{lo},{hi}] vs threshold {threshold}"
            )
    instance = (evidence.get("witness") or {}).get("instance")
    if classification in ("ALL", "PARTIAL") and instance is not None:
        overlap = Fraction(instance["overlap"])
        meets = instance.get("meets_threshold")
        if meets != (overlap >= threshold):
            problems.append("witness meets_threshold disagrees with its overlap")
        if instance.get("start_difference") is not None:
            d = Fraction(instance["start_difference"])
            from .calibration_algo import overlap_for_delta

            dt1 = Fraction(document["request"]["exposure_1"]["duration"])
            dt2 = Fraction(document["request"]["exposure_2"]["duration"])
            if overlap_for_delta(d, dt1, dt2) != overlap:
                problems.append("witness overlap disagrees with its start difference")
    return problems


def public_semantic_product(semantics: SemanticsService, product_id: str) -> dict:
    product = semantics.get_semantic_product(product_id)
    return {
        "kind": "semantic_product",
        "product_id": product["id"],
        "reference_version": product["reference_version"],
        "reference_digest": product["reference_digest"],
        "content_digest": product["content_digest"],
        "sample_ids": product["sample_ids"],
        "records": [
            {
                "sample_id": item["sample_id"],
                "received_revision": item["received_revision"],
                "original_bits": item["bits"],
                "kind": item["kind"],
                "sign": item["sign"],
                "payload": item["payload"],
                "exact_value": item["value"],
                "negated_value": item["negated_value"],
                "expected": item["expected"],
                "tolerance": item["tolerance"],
                "status": item["status"],
                "difference": item["difference"],
                "abs_difference": item["abs_difference"],
            }
            for item in product["items"]
        ],
    }


def public_delivery(delivery: DeliveryService, delivery_id: str) -> dict:
    view = delivery.get_delivery(delivery_id)
    return {
        "kind": "delivery",
        "delivery_id": view["delivery_id"],
        "canonical_product": {
            "product_id": view["product_id"],
            "content_digest": view["product_digest"],
        },
        "reference": {
            "version": view["reference_version"],
            "content_digest": view["reference_digest"],
        },
        "reader_contract": view["contract"],
        "contract_id": view["contract_id"],
        "content_digest": view["content_digest"],
        "lines": [
            {
                "sample_id": line["sample_id"],
                "original_bits": line["original_bits"],
                "kind": line["kind"],
                "sign": line["sign"],
                "original_exact_value": line["exact_value"],
                "original_negated_value": line["negated_value"],
                "semantic_status": line["status"],
                "delivered_status": line["delivered_status"],
                "coefficient": line["coefficient"],
                "scale": line["scale"],
                "reader_value": line["reader_value"],
                "exact_error": line["error"],
                "decoder": "coefficient / 10**scale",
                "evidence": line["evidence"],
            }
            for line in view["lines"]
        ],
    }


def verify_public_delivery(document: dict) -> list[str]:
    """Re-derive all checks from a standalone public document.

    Returns a list of textual problems (empty means the document is
    internally consistent).  It does not need a database: it recomputes the
    coefficient bound, error budget, scale legality and reader value.
    """
    problems: list[str] = []
    contract = document["reader_contract"]
    allowed = set(contract["allowed_scales"])
    bound = contract["max_abs_coefficient"]
    budget = Fraction(contract["max_abs_error"])
    required = {
        "sample_id", "original_bits", "kind", "sign", "original_exact_value",
        "semantic_status", "delivered_status", "coefficient", "scale",
        "reader_value", "exact_error",
    }
    for line in document["lines"]:
        missing = required - line.keys()
        if missing:
            problems.append(f"{line.get('sample_id')}: missing {sorted(missing)}")
            continue
        sample = line["sample_id"]
        scale = line["scale"]
        coefficient = line["coefficient"]
        if scale not in allowed:
            problems.append(f"{sample}: scale {scale} not allowed")
        if abs(coefficient) > bound:
            problems.append(f"{sample}: coefficient {coefficient} exceeds {bound}")
        if line["semantic_status"] != line["delivered_status"]:
            problems.append(f"{sample}: semantic status changed on delivery")
        value = Fraction(line["original_exact_value"])
        delivered = Fraction(coefficient, 10 ** scale)
        error = delivered - value
        if str(Fraction(line["exact_error"])) != str(error):
            problems.append(f"{sample}: stored error {line['exact_error']} != {error}")
        if abs(error) > budget:
            problems.append(f"{sample}: error {error} exceeds budget {budget}")
        if str(Fraction(line["reader_value"])) != str(delivered):
            problems.append(f"{sample}: reader value mismatch")
    return problems


def write_json(path: str | Path, document: dict) -> Path:
    target = Path(path)
    target.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
    return target
