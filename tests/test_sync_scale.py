"""Scale test for the calibration / paired-delivery subsystem.

Hundreds of pulses, several segments with multiple moment associations, and a
paired delivery read back after a database reopen.  Everything stays exact.
"""

from __future__ import annotations

import time
from fractions import Fraction

from obsdelivery.calibration import CalibrationService
from obsdelivery.pairing import PairDeliveryService
from obsdelivery.public import (
    public_calibration_product,
    public_paired_delivery,
    verify_public_calibration,
    verify_public_pair,
)
from obsdelivery.storage import Store


def _bits_for_int(value: int) -> str:
    """Exact finite binary32 bit pattern for a small non-negative integer."""
    if value == 0:
        return "00000000"
    # integers up to 2^24 are exactly representable: exponent chosen so the
    # implicit significand is the integer and the exponent is zero.
    exponent = 127 + value.bit_length() - 1
    frac = (value << (23 - (value.bit_length() - 1))) - (1 << 23)
    raw = (exponent << 23) | frac
    return f"{raw:08x}"


def test_many_pulses_multi_moment_and_pair_readback(tmp_path):
    path = str(tmp_path / "scale-sync.sqlite")
    store = Store(path)
    cal = CalibrationService(store)
    pair = PairDeliveryService(store, cal)

    start = time.monotonic()
    # Segment with a unit-slope band; many pulses pin the intercept near 0.
    cal.register_segment("many", "A", "1", "1")
    pulses = [
        {"pulse_id": f"p{i}", "revision": 1, "tick": i,
         "bits": _bits_for_int(i), "halfwidth": "1/2"}
        for i in range(0, 300, 10)
    ]
    cal.admit_pulses("many", pulses)
    ticks = list(range(0, 31))
    product = cal.publish_calibration_product("PM", "many", ticks=ticks)
    assert product["feasible_slope"] == {"lo": "1", "hi": "1"}
    # 31 ticks -> 31*30/2 pairwise associations, all with unit-slope difference.
    assert len(product["moments"]["differences"]) == len(ticks) * (len(ticks) - 1) // 2
    for d in product["moments"]["differences"]:
        assert Fraction(d["difference_lo"]) == d["to_tick"] - d["from_tick"]
    assert verify_public_calibration(public_calibration_product(cal, "PM")) == []

    # A second independent segment and a paired request across the two.
    cal.register_segment("other", "B", "1", "1")
    cal.admit_pulses("other", [
        {"pulse_id": "q0", "revision": 1, "tick": 0, "bits": "3f800000", "halfwidth": "1"}
    ])
    cal.publish_calibration_product("PO", "other", ticks=[0])

    req = {
        "exposure_1": {"product_id": "PM", "start_tick": 0, "duration": 5},
        "exposure_2": {"product_id": "PO", "start_tick": 0, "duration": 5},
        "min_common": "4",
    }
    verdict = pair.submit("PSCALE", req)
    assert verdict["classification"] in ("ALL", "PARTIAL")
    elapsed = time.monotonic() - start
    assert elapsed < 30
    assert verify_public_pair(public_paired_delivery(pair, "PSCALE")) == []
    store.close()

    # Reopen: the pair replays and the product is byte-identical.
    reopened = Store(path)
    cal2 = CalibrationService(reopened)
    pair2 = PairDeliveryService(reopened, cal2)
    again = pair2.submit("PSCALE", req)
    assert again["created"] is False
    assert again["content_digest"] == verdict["content_digest"]
    assert cal2.read_calibration_product("PM")["content_digest"] == product["content_digest"]
    reopened.close()


def test_hundreds_of_mutually_constraining_pulses():
    """201 exact pulses pin the slope to exactly 1 across a wide band."""
    store = Store(":memory:")
    cal = CalibrationService(store)
    cal.register_segment("s", "A", "1/100", "100")
    # Pin (tick i -> arrival i) for i in 0..200; only slope 1, intercept 0 fits.
    cal.admit_pulses("s", [
        {"pulse_id": f"p{i}", "revision": 1, "tick": i,
         "bits": _bits_for_int(i), "halfwidth": "0"}
        for i in range(201)
    ])
    feas = cal.solve("s")
    assert feas.consistent
    assert feas.feasible_lo == feas.feasible_hi == 1
    assert feas.witness_a == 1 and feas.witness_b == 0
    store.close()
