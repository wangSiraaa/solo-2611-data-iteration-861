"""Segment calibration, pulse revisions and paired same-shot delivery."""

from fractions import Fraction

import pytest

from obsdelivery.calibration import CalibrationService
from obsdelivery.pairing import ALL, NONE, PARTIAL, PairDeliveryService
from obsdelivery.calibration_algo import (
    Pulse,
    classify_independent,
    classify_shared,
    evaluate_calibration,
    overlap_for_delta,
    tick_value_bounds,
    verify_witness,
)
from obsdelivery.errors import (
    BatchRejected,
    CalibrationConflict,
    CalibrationNotFound,
    CalibrationRejected,
    PairDeliveryNotSatisfied,
    PairIdConflict,
    PairNotFound,
    PairRequestRejected,
    ValidationError,
)
from obsdelivery.public import (
    public_calibration_product,
    public_paired_delivery,
    verify_public_calibration,
    verify_public_pair,
)
from obsdelivery.storage import Store


@pytest.fixture()
def env(tmp_path):
    store = Store(str(tmp_path / "cal.sqlite"))
    cal = CalibrationService(store)
    pair = PairDeliveryService(store, cal)
    yield store, cal, pair
    store.close()


def pulse(pid, tick, bits, halfwidth, revision=1):
    return {
        "pulse_id": pid,
        "revision": revision,
        "tick": tick,
        "bits": bits,
        "halfwidth": halfwidth,
    }


ONE = "3f800000"   # exact 1
ZERO = "00000000"  # exact 0
TEN = "41200000"   # exact 10


# ---------------------------------------------------------------------------
# pure algorithm
# ---------------------------------------------------------------------------

def test_overlap_for_delta_matches_interval_intersection():
    # [0,3] and [d, d+3]
    assert overlap_for_delta(Fraction(0), Fraction(3), Fraction(3)) == 3
    assert overlap_for_delta(Fraction(1), Fraction(3), Fraction(3)) == 2
    assert overlap_for_delta(Fraction(-1), Fraction(3), Fraction(3)) == 2
    assert overlap_for_delta(Fraction(3), Fraction(3), Fraction(3)) == 0
    assert overlap_for_delta(Fraction(4), Fraction(3), Fraction(3)) == 0
    # Different durations: [0,5] vs [1,3] -> overlap 2.
    assert overlap_for_delta(Fraction(1), Fraction(5), Fraction(2)) == 2
    assert overlap_for_delta(Fraction(-1), Fraction(2), Fraction(5)) == 2


def test_evaluate_calibration_slope_band_from_opposing_pulses():
    # p0 at tick 0 in [-1/2, 1/2]; p10 at tick 10 in [19/2, 21/2].
    # slope bounds come from the tightest cross pairs.
    pulses = [
        Pulse("p0", 0, Fraction(-1, 2), Fraction(1, 2)),
        Pulse("p10", 10, Fraction(19, 2), Fraction(21, 2)),
    ]
    feas = evaluate_calibration(Fraction(1, 2), Fraction(2), pulses)
    assert feas.consistent
    # lower = (lo_10 - hi_0)/10 = (19/2 - 1/2)/10 = 9/10
    assert feas.feasible_lo == Fraction(9, 10)
    # upper = (hi_10 - lo_0)/10 = (21/2 + 1/2)/10 = 11/10
    assert feas.feasible_hi == Fraction(11, 10)
    verify_witness(feas, pulses)


def test_evaluate_calibration_detects_contradiction_with_evidence():
    # tick 0 pinned to 0, tick 10 pinned to 10 -> slope must be 1.
    pinned = [
        Pulse("p0", 0, Fraction(0), Fraction(0)),
        Pulse("p10", 10, Fraction(10), Fraction(10)),
    ]
    ok = evaluate_calibration(Fraction(1, 2), Fraction(2), pinned)
    assert ok.consistent and ok.feasible_lo == ok.feasible_hi == 1

    # Now demand tick 10 arrives at 20 -> slope 2, but the requested band is
    # [1/2, 3/2]; the empty interval exposes the offending pair.
    tighter = evaluate_calibration(
        Fraction(1, 2), Fraction(3, 2),
        [Pulse("p0", 0, Fraction(0), Fraction(0)),
         Pulse("p10", 10, Fraction(20), Fraction(20))],
    )
    assert not tighter.consistent
    reason = tighter.reasons[0]
    assert reason["reason"] == "slope_interval_empty"
    assert Fraction(reason["lower"]) > Fraction(reason["upper"])


def test_same_tick_pulses_must_share_a_point():
    pulses = [
        Pulse("a", 0, Fraction(0), Fraction(1)),
        Pulse("b", 0, Fraction(3), Fraction(4)),
    ]
    feas = evaluate_calibration(Fraction(1), Fraction(1), pulses)
    assert not feas.consistent
    assert feas.reasons[0]["reason"] == "same_tick_interval_empty"


def test_tick_value_bounds_and_association():
    pulses = [
        Pulse("p0", 0, Fraction(-1, 2), Fraction(1, 2)),
        Pulse("p10", 10, Fraction(19, 2), Fraction(21, 2)),
    ]
    feas = evaluate_calibration(Fraction(1, 2), Fraction(2), pulses)
    lo, hi = tick_value_bounds(feas, pulses, 0)
    # offset at tick 0 ranges over the pulse interval itself.
    assert lo == Fraction(-1, 2) and hi == Fraction(1, 2)
    lo5, hi5 = tick_value_bounds(feas, pulses, 5)
    assert lo5 < hi5


def test_classify_shared_and_independent_are_exact():
    feas = evaluate_calibration(
        Fraction(1), Fraction(1),
        [Pulse("p0", 0, Fraction(0), Fraction(1))],
    )
    # shared, ticks 0 and 1, unit slope -> start difference exactly 1.
    shared = classify_shared(
        feas, [Pulse("p0", 0, Fraction(0), Fraction(1))],
        0, Fraction(3), 1, Fraction(3), Fraction(2),
    )
    assert shared["classification"] == ALL
    assert shared["min_overlap"] == shared["max_overlap"] == 2


def test_independent_witness_is_admissible_and_optimal():
    from obsdelivery.calibration_algo import independent_witness

    def admissible(pulses, wit):
        a, b = Fraction(wit["a"]), Fraction(wit["b"])
        return all(p.lo <= a * p.tick + b <= p.hi for p in pulses)

    # Free intercept (no pulses): can align exactly -> full overlap.
    free = evaluate_calibration(Fraction(1), Fraction(1), [])
    wit = independent_witness(free, [], 0, Fraction(3), free, [], 5, Fraction(2), Fraction(2))
    assert Fraction(wit["overlap"]) == 2
    assert Fraction(wit["start_difference"]) == 0

    # Disjoint pinned starts -> overlap genuinely zero, yet witness is allowed.
    p1 = [Pulse("a", 0, Fraction(0), Fraction(0))]
    p2 = [Pulse("b", 0, Fraction(10), Fraction(10))]
    f1 = evaluate_calibration(Fraction(1), Fraction(1), p1)
    f2 = evaluate_calibration(Fraction(1), Fraction(1), p2)
    verdict = classify_independent(f1, p1, 0, Fraction(2), f2, p2, 0, Fraction(2), Fraction(1))
    assert verdict["classification"] == NONE
    wit = independent_witness(f1, p1, 0, Fraction(2), f2, p2, 0, Fraction(2), Fraction(1))
    assert admissible(p1, wit["calibration_1"])
    assert admissible(p2, wit["calibration_2"])
    assert Fraction(wit["overlap"]) == 0


def test_independent_witness_reaches_true_maximum():
    """Regression: the optimum need not sit on a slope kink of either band."""
    from obsdelivery.calibration_algo import independent_witness as iw

    p1 = [
        Pulse("a", -2, Fraction(-3), Fraction(-1)),
        Pulse("b", 6, Fraction(2), Fraction(2)),
    ]
    p2 = [
        Pulse("a", 0, Fraction(-2), Fraction(2)),
        Pulse("b", 2, Fraction(-1), Fraction(-1)),
    ]
    f1 = evaluate_calibration(Fraction(1, 4), Fraction(4), p1)
    f2 = evaluate_calibration(Fraction(1, 4), Fraction(4), p2)
    acc = iw(f1, p1, 0, Fraction(2), f2, p2, 0, Fraction(2), Fraction(1))
    # The classifier says the best possible overlap is 2; the witness must too.
    cls = classify_independent(f1, p1, 0, Fraction(2), f2, p2, 0, Fraction(2), Fraction(1))
    assert Fraction(acc["overlap"]) == Fraction(cls["max_overlap"]) == 2
    a1, b1 = Fraction(acc["calibration_1"]["a"]), Fraction(acc["calibration_1"]["b"])
    a2, b2 = Fraction(acc["calibration_2"]["a"]), Fraction(acc["calibration_2"]["b"])
    assert all(q.lo <= a1 * q.tick + b1 <= q.hi for q in p1)
    assert all(q.lo <= a2 * q.tick + b2 <= q.hi for q in p2)


# ---------------------------------------------------------------------------
# admission + product persistence
# ---------------------------------------------------------------------------

def test_pulse_revision_rules_mirror_observations(env):
    _, cal, _ = env
    cal.register_segment("s", "A", "1", "1")
    first = cal.admit_pulses("s", [pulse("p", 0, ZERO, "1")])
    assert first["accepted_count"] == 1
    assert first["members"][0]["disposition"] == "new"

    # Same revision, same content -> replay, no new effective state.
    replay = cal.admit_pulses("s", [pulse("p", 0, ZERO, "1")])
    assert replay["created"] is False

    # Same revision, different content -> whole batch rejected atomically.
    with pytest.raises(BatchRejected):
        cal.admit_pulses("s", [pulse("p", 0, ONE, "1")])
    assert len(cal.effective_pulses("s")) == 1
    assert cal.effective_pulses("s")[0].lo == -1

    # Higher revision wins; lower revision is accepted but ignored.
    cal.admit_pulses("s", [pulse("p", 0, ONE, "1/2", revision=5)])
    eff = cal.effective_pulses("s")[0]
    assert eff.revision == 5 and eff.lo == Fraction(1, 2) and eff.hi == Fraction(3, 2)
    low = cal.admit_pulses("s", [pulse("p", 0, ZERO, "0", revision=2)])
    assert low["members"][0]["disposition"] == "ignored_older"
    assert cal.effective_pulses("s")[0].revision == 5


def test_nonfinite_pulse_reference_rejected(env):
    _, cal, _ = env
    cal.register_segment("s", "A", "1", "1")
    with pytest.raises(ValidationError):
        cal.admit_pulses("s", [pulse("p", 0, "7fc00001", "1")])


def test_product_persists_pulses_and_reads_back_after_reopen(env, tmp_path):
    store, cal, pair = env
    cal.register_segment("s", "A", "1/2", "2")
    cal.admit_pulses("s", [pulse("p0", 0, ZERO, "1/2"), pulse("p10", 10, TEN, "1/2")])
    product = cal.publish_calibration_product("PX", "s", ticks=[0, 5, 10])
    assert product["feasible_slope"] == {"lo": "9/10", "hi": "11/10"}
    digest = product["content_digest"]

    # A calibration product that requires several moments records how they
    # move together (differences depend only on the shared slope).
    diffs = {(d["from_tick"], d["to_tick"]): (d["difference_lo"], d["difference_hi"])
             for d in product["moments"]["differences"]}
    assert diffs[(0, 10)] == ("9", "11")
    assert diffs[(0, 5)] == ("9/2", "11/2")

    store.close()
    reopened = Store(str(tmp_path / "cal.sqlite"))
    cal2 = CalibrationService(reopened)
    back = cal2.read_calibration_product("PX")
    assert back["content_digest"] == digest
    assert back["pulses"] == product["pulses"]
    assert back["feasible_slope"] == product["feasible_slope"]
    reopened.close()
    # Restore the fixture store handle for teardown.
    env[0].conn = store.conn if not store.conn else env[0].conn


def test_inconsistent_new_product_refused_but_history_readable(env):
    _, cal, pair = env
    cal.register_segment("s", "A", "1/2", "2")
    cal.admit_pulses("s", [pulse("p0", 0, ZERO, "1/2"), pulse("p10", 10, TEN, "1/2")])
    good = cal.publish_calibration_product("P-good", "s", ticks=[0, 10])

    # A contradictory revision pins both pulses to incompatible exact values.
    cal.admit_pulses("s", [pulse("p0", 0, ZERO, "0", revision=2)])
    cal.admit_pulses("s", [pulse("p10", 10, TEN, "0", revision=2)])
    # p0 exact 0 and p10 exact 10 -> slope exactly 1, which is consistent.
    assert cal.solve("s").consistent
    # Now push p10 to an arrival requiring slope 2, outside [1/2, 2]? slope 2
    # is allowed, so tighten the segment instead via a new station-free pulse.
    cal.register_segment("t", "B", "1", "1")
    cal.admit_pulses("t", [pulse("a", 0, ZERO, "0"), pulse("b", 10, TEN, "0")])
    # Force inconsistency: same tick, disjoint exact arrivals.
    cal.admit_pulses("t", [pulse("c", 0, ONE, "0")])
    with pytest.raises(CalibrationRejected) as exc:
        cal.publish_calibration_product("P-bad", "t")
    assert exc.value.reasons[0]["reason"] == "same_tick_interval_empty"
    # No product row for the refused id.
    with pytest.raises(CalibrationNotFound):
        cal.get_calibration_product("P-bad")
    assert [a["status"] for a in cal.attempts("P-bad")] == ["rejected"]
    # History is untouched.
    assert cal.get_calibration_product("P-good")["content_digest"] == good["content_digest"]


def test_calibration_conflict_on_same_id_different_content(env):
    _, cal, _ = env
    cal.register_segment("s", "A", "1", "1")
    cal.admit_pulses("s", [pulse("p", 0, ZERO, "1")])
    cal.publish_calibration_product("P", "s")
    cal.admit_pulses("s", [pulse("p", 0, ZERO, "0", revision=2)])
    with pytest.raises(CalibrationConflict):
        cal.publish_calibration_product("P", "s")


def test_segment_registration_is_immutable(env):
    _, cal, _ = env
    seg = cal.register_segment("s", "A", "1", "1")
    assert seg["station_id"] == "A"
    cal.register_segment("s", "A", "1", "1")  # replay ok
    with pytest.raises(CalibrationConflict):
        cal.register_segment("s", "B", "1", "1")
    with pytest.raises(ValidationError):
        cal.register_segment("z", "C", "0", "1")  # slope must be positive


# ---------------------------------------------------------------------------
# the four worked scenarios + the non-unit-slope scenario
# ---------------------------------------------------------------------------

def _offset_segment(env, seg, halfwidth):
    _, cal, _ = env
    cal.register_segment(seg, seg.upper(), "1", "1")
    cal.admit_pulses(seg, [pulse("p0", 0, ONE, halfwidth)])
    return cal.publish_calibration_product(f"P-{seg}", seg, ticks=[0])


def test_scenario_partial_then_guaranteed(env):
    _, cal, pair = env
    pa = _offset_segment(env, "a", "1")   # offset in [0,2]
    pb = _offset_segment(env, "b", "1")

    req = {
        "exposure_1": {"product_id": pa["product_id"], "start_tick": 0, "duration": 3},
        "exposure_2": {"product_id": pb["product_id"], "start_tick": 0, "duration": 3},
        "min_common": "2",
    }
    ev = pair.evaluate(req)
    assert ev["classification"] == PARTIAL
    assert ev["min_overlap"] == "1" and ev["max_overlap"] == "3"
    # The witness instance reaches the maximum and is checkable.
    inst = ev["witness"]["instance"]
    assert Fraction(inst["overlap"]) == 3

    # Tighten both pulses to offset 1 exactly -> every calibration passes.
    cal.admit_pulses("a", [pulse("p0", 0, ONE, "0", revision=2)])
    cal.admit_pulses("b", [pulse("p0", 0, ONE, "0", revision=2)])
    pa2 = cal.publish_calibration_product("Pa2", "a", ticks=[0])
    pb2 = cal.publish_calibration_product("Pb2", "b", ticks=[0])
    req2 = dict(req)
    req2["exposure_1"] = {"product_id": pa2["product_id"], "start_tick": 0, "duration": 3}
    req2["exposure_2"] = {"product_id": pb2["product_id"], "start_tick": 0, "duration": 3}
    ev2 = pair.evaluate(req2)
    assert ev2["classification"] == ALL
    assert ev2["min_overlap"] == ev2["max_overlap"] == "3"


def test_scenario_shared_segment_thresholds(env):
    _, cal, pair = env
    cal.register_segment("s", "A", "1", "1")
    cal.admit_pulses("s", [pulse("p0", 0, ONE, "1")])
    cal.publish_calibration_product("PS", "s", ticks=[0, 1])

    base = {
        "exposure_1": {"product_id": "PS", "start_tick": 0, "duration": 3},
        "exposure_2": {"product_id": "PS", "start_tick": 1, "duration": 3},
    }
    at2 = pair.evaluate({**base, "min_common": "2"})
    assert at2["classification"] == ALL
    assert at2["min_overlap"] == at2["max_overlap"] == "2"
    assert at2["shared_calibration"] is True

    at22 = pair.evaluate({**base, "min_common": "5/2"})
    assert at22["classification"] == NONE
    assert Fraction(at22["max_overlap"]) == 2 < Fraction(5, 2)

    # NONE is refused at submit; the evidence proves it is checkable.
    with pytest.raises(PairDeliveryNotSatisfied) as exc:
        pair.submit("P-none", {**base, "min_common": "5/2"})
    assert Fraction(exc.value.evidence["max_overlap"]) < Fraction(exc.value.evidence["threshold"])
    with pytest.raises(PairNotFound):
        pair.get_pair("P-none")

    # ALL is published and replays.
    out = pair.submit("P-ok", {**base, "min_common": "2"})
    assert out["classification"] == ALL
    assert pair.submit("P-ok", {**base, "min_common": "2"})["created"] is False
    with pytest.raises(PairIdConflict):
        pair.submit("P-ok", {**base, "min_common": "5/2"})


def test_scenario_non_unit_slope_and_mutual_pulses(env):
    _, cal, pair = env
    cal.register_segment("s", "X", "1/2", "2")
    cal.admit_pulses(
        "s",
        [pulse("p0", 0, ZERO, "1/2"), pulse("p10", 10, TEN, "1/2")],
    )
    px = cal.publish_calibration_product("PX", "s", ticks=[0, 5, 10])
    assert px["feasible_slope"] == {"lo": "9/10", "hi": "11/10"}

    cal.register_segment("t", "Y", "1", "1")
    cal.admit_pulses("t", [pulse("q0", 0, ZERO, "1")])
    cal.publish_calibration_product("PY", "t", ticks=[0])

    req = {
        "exposure_1": {"product_id": "PX", "start_tick": 0, "duration": 4},
        "exposure_2": {"product_id": "PY", "start_tick": 0, "duration": 4},
        "min_common": "3",
    }
    ev = pair.evaluate(req)
    # X starts in [-1/2,1/2], Y in [-1,1]; zero difference is admissible so
    # the maximum overlap is the full 4.  The minimum comes from the worst
    # far-apart start difference.
    assert ev["classification"] == PARTIAL
    assert ev["max_overlap"] == "4"
    assert Fraction(ev["min_overlap"]) == Fraction(5, 2)
    assert ev["witness"]["instance"]["meets_threshold"] is True

    out = pair.submit("P5", req)
    assert out["mode"] == "independent" and out["classification"] == PARTIAL
    # Public read-back re-derives the verdict without the database.
    assert verify_public_pair(public_paired_delivery(pair, "P5")) == []
    assert verify_public_calibration(public_calibration_product(cal, "PX")) == []


# ---------------------------------------------------------------------------
# public read-back and tamper detection
# ---------------------------------------------------------------------------

def test_public_calibration_rejects_tampering(env):
    _, cal, _ = env
    cal.register_segment("s", "A", "1", "1")
    cal.admit_pulses("s", [pulse("p0", 0, ONE, "1")])
    cal.publish_calibration_product("P", "s", ticks=[0])
    doc = public_calibration_product(cal, "P")
    assert verify_public_calibration(doc) == []

    bad_slope = {**doc, "feasible_slope": {"lo": "0", "hi": "3"}}
    assert verify_public_calibration(bad_slope)
    bad_witness = {**doc, "witness": {"a": "2", "b": "0"}}
    assert verify_public_calibration(bad_witness)
    bad_moments = {**doc, "moments": {"ticks": [0], "per_tick": {}, "differences": []}}
    assert verify_public_calibration(bad_moments)


def test_public_pair_rejects_classification_laundering(env):
    _, cal, pair = env
    cal.register_segment("s", "A", "1", "1")
    cal.admit_pulses("s", [pulse("p0", 0, ONE, "1")])
    cal.publish_calibration_product("PS", "s", ticks=[0, 1])
    req = {
        "exposure_1": {"product_id": "PS", "start_tick": 0, "duration": 3},
        "exposure_2": {"product_id": "PS", "start_tick": 1, "duration": 3},
        "min_common": "5/2",
    }
    # Cannot publish NONE, so build a synthetic "published" doc to attack.
    ev = pair.evaluate(req)
    doc = {
        "kind": "paired_delivery",
        "pair_id": "fake",
        "classification": "ALL",
        "shared_calibration": True,
        "request": ev["request"],
        "evidence": ev,
    }
    # The overlap extremes are [2,2] and the threshold is 5/2, so claiming ALL
    # is caught independently.
    assert verify_public_pair(doc)


def test_same_segment_requires_one_calibration(env):
    _, cal, pair = env
    cal.register_segment("s", "A", "1", "1")
    cal.admit_pulses("s", [pulse("p0", 0, ZERO, "1")])
    cal.publish_calibration_product("P-one", "s", ticks=[0, 1])
    cal.publish_calibration_product("P-two", "s", ticks=[0, 1])  # same content, different id

    same = {
        "exposure_1": {"product_id": "P-one", "start_tick": 0, "duration": 3},
        "exposure_2": {"product_id": "P-two", "start_tick": 1, "duration": 3},
        "min_common": "2",
    }
    # Two products of the same segment that agree exactly may share the band.
    ev = pair.evaluate(same)
    assert ev["shared_calibration"] is True
    assert ev["classification"] == ALL

    # Publish a product of the same segment with a *different* pulse set.
    cal.admit_pulses("s", [pulse("p0", 0, ONE, "0", revision=2)])
    cal.publish_calibration_product("P-three", "s", ticks=[0, 1])
    mismatched = {
        "exposure_1": {"product_id": "P-one", "start_tick": 0, "duration": 3},
        "exposure_2": {"product_id": "P-three", "start_tick": 1, "duration": 3},
        "min_common": "2",
    }
    with pytest.raises(PairRequestRejected):
        pair.evaluate(mismatched)


def test_request_validation(env):
    _, cal, pair = env
    _offset_segment(env, "a", "0")
    good = {
        "exposure_1": {"product_id": "P-a", "start_tick": 0, "duration": 3},
        "exposure_2": {"product_id": "P-a", "start_tick": 0, "duration": 3},
        "min_common": "1",
    }
    assert pair.evaluate(good)["classification"] == ALL
    for broken in [
        {**good, "min_common": "0"},
        {**good, "exposure_1": {"product_id": "P-a", "start_tick": 0, "duration": "0"}},
        {**good, "exposure_1": {"product_id": "missing", "start_tick": 0, "duration": 3}},
    ]:
        with pytest.raises((ValidationError, CalibrationNotFound)):
            pair.evaluate(broken)


def test_old_paired_delivery_survives_new_products(env, tmp_path):
    store, cal, pair = env
    cal.register_segment("s", "A", "1", "1")
    cal.admit_pulses("s", [pulse("p0", 0, ZERO, "1")])
    cal.publish_calibration_product("P1", "s", ticks=[0])
    req = {
        "exposure_1": {"product_id": "P1", "start_tick": 0, "duration": 2},
        "exposure_2": {"product_id": "P1", "start_tick": 0, "duration": 2},
        "min_common": "1",
    }
    old = pair.submit("OLD", req)
    assert old["classification"] == ALL
    # A later, contradicting revision forces a new product to be refused.
    cal.admit_pulses("s", [pulse("p0", 0, ZERO, "0", revision=2)])
    cal.admit_pulses("s", [pulse("add", 0, ONE, "0", revision=2)])
    with pytest.raises(CalibrationRejected):
        cal.publish_calibration_product("P2", "s", ticks=[0])
    assert pair.get_pair("OLD")["content_digest"] == old["content_digest"]


def test_pair_request_reproducible_across_reopen(env, tmp_path):
    store, cal, pair = env
    cal.register_segment("s", "A", "1", "1")
    cal.admit_pulses("s", [pulse("p0", 0, ONE, "1")])
    cal.publish_calibration_product("PS", "s", ticks=[0, 1])
    req = {
        "exposure_1": {"product_id": "PS", "start_tick": 0, "duration": 3},
        "exposure_2": {"product_id": "PS", "start_tick": 1, "duration": 3},
        "min_common": "2",
    }
    out = pair.submit("PP", req)
    digest = out["content_digest"]
    store.close()

    reopened = Store(str(tmp_path / "cal.sqlite"))
    pair2 = PairDeliveryService(reopened, CalibrationService(reopened))
    again = pair2.submit("PP", req)
    assert again["created"] is False
    assert again["content_digest"] == digest
    assert again["evidence"]["classification"] == ALL
    reopened.close()


def test_old_store_without_sync_tables_upgrades_additively(tmp_path):
    """A store written before this feature keeps its rows; new tables appear."""
    from obsdelivery.contracts import ReaderContract
    from obsdelivery.delivery import DeliveryService
    from obsdelivery.service import SemanticsService

    path = str(tmp_path / "legacy.sqlite")
    store = Store(path)
    semantics = SemanticsService(store)
    delivery = DeliveryService(store, semantics)
    semantics.admit_observations([{"sample_id": "s1", "revision": 1, "bits": "3dcccccd"}])
    semantics.publish_reference(
        "r1", [{"sample_id": "s1", "expected": "1/10", "tolerance": "0"}], copy_from=None
    )
    semantics.publish_semantic_product("p1", ["s1"], "r1")
    contract = ReaderContract.from_mapping(
        {"allowed_scales": [2, 3], "max_abs_coefficient": 99, "max_abs_error": "1/200"}
    )
    delivery_digest = delivery.submit("D1", "p1", ["s1"], contract)["content_digest"]
    product_digest = semantics.get_semantic_product("p1")["content_digest"]

    # Emulate a pre-feature schema by dropping the additive tables.
    for table in [
        "paired_delivery_attempts", "paired_deliveries", "calibration_attempts",
        "calibration_product_pulse", "calibration_products", "calibration_pulses",
        "calibration_pulse_batch_member", "calibration_pulse_admissions",
        "calibration_segments",
    ]:
        store.conn.execute(f"DROP TABLE IF EXISTS {table}")
    store.close()

    reopened = Store(path)  # runs the schema, recreating the missing tables
    semantics2 = SemanticsService(reopened)
    delivery2 = DeliveryService(reopened, semantics2)
    assert delivery2.get_delivery("D1")["content_digest"] == delivery_digest
    assert semantics2.get_semantic_product("p1")["content_digest"] == product_digest

    # The new subsystem works on the upgraded store.
    cal = CalibrationService(reopened)
    pair = PairDeliveryService(reopened, cal)
    cal.register_segment("seg", "A", "1", "1")
    cal.admit_pulses("seg", [pulse("p0", 0, ONE, "1")])
    cal.publish_calibration_product("CP", "seg", ticks=[0])
    verdict = pair.submit(
        "NP",
        {
            "exposure_1": {"product_id": "CP", "start_tick": 0, "duration": 2},
            "exposure_2": {"product_id": "CP", "start_tick": 0, "duration": 2},
            "min_common": "1",
        },
    )
    assert verdict["classification"] == ALL
    reopened.close()
