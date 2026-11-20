"""Exact segment-calibration feasibility and paired-exposure overlap (pure).

A station maps its local integer ``tick`` to station-external time inside one
acquisition segment by an *unknown* rational affine map::

    T = a * tick + b

The slope ``a`` ranges over a positive closed interval ``[slope_lo, slope_hi]``
(``0 < slope_lo <= slope_hi``).  The intercept ``b`` is otherwise free and is
constrained only by time-sync pulses.  Each pulse contributes one constraint::

    pulse_lo <= a * pulse_tick + b <= pulse_hi

where ``[pulse_lo, pulse_hi]`` is the closed interval of true arrival times.

Everything here uses :class:`fractions.Fraction`; no host float is involved and
the module knows nothing about SQLite.

Why this is exact and small
---------------------------
Eliminating ``b`` turns consistency into a one-dimensional problem.  For a
fixed ``a`` the feasible intercepts are::

    [ max_i(pulse_lo_i - a*pulse_tick_i),
      min_i(pulse_hi_i - a*pulse_tick_i) ]

so the system is consistent iff that interval is non-empty for some ``a`` in
the slope interval.  Comparing every ordered pair of pulses collapses this to
a *feasible slope interval*::

    pulse_lo_i - pulse_hi_j
    -----------------------   (for pulse_tick_i > pulse_tick_j)
    pulse_tick_i - pulse_tick_j

as a lower bound, and the same ratio for ``pulse_tick_i < pulse_tick_j`` as an
upper bound, plus a same-tick check (all pulses sharing a tick must have a
common arrival point).  The feasible ``a`` set is therefore one closed
interval, which makes classifying exposure overlap a matter of evaluating a
piecewise-linear function at a handful of exact breakpoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Sequence

from .canonical import frac_str


# ---------------------------------------------------------------------------
# value objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Pulse:
    """One effective time-sync pulse: an integer tick and an arrival interval."""

    pulse_id: str
    tick: int
    lo: Fraction
    hi: Fraction
    revision: int = 0
    bits: str = ""

    def canonical(self) -> dict:
        return {
            "pulse_id": self.pulse_id,
            "tick": self.tick,
            "lo": self.lo,
            "hi": self.hi,
            "revision": self.revision,
            "bits": self.bits,
        }


@dataclass(frozen=True)
class Feasibility:
    """Result of solving the pulse constraints for one segment."""

    consistent: bool
    requested_lo: Fraction
    requested_hi: Fraction
    feasible_lo: Fraction
    feasible_hi: Fraction
    witness_a: Fraction | None
    witness_b: Fraction | None
    reasons: tuple[dict, ...]

    def as_dict(self) -> dict:
        return {
            "consistent": self.consistent,
            "requested_slope": {"lo": self.requested_lo, "hi": self.requested_hi},
            "feasible_slope": {"lo": self.feasible_lo, "hi": self.feasible_hi},
            "witness": (
                None
                if self.witness_a is None
                else {"a": self.witness_a, "b": self.witness_b}
            ),
            "reasons": list(self.reasons),
        }


# ---------------------------------------------------------------------------
# consistency
# ---------------------------------------------------------------------------

def evaluate_calibration(
    slope_lo: Fraction,
    slope_hi: Fraction,
    pulses: Sequence[Pulse],
) -> Feasibility:
    """Decide whether the affine map with pulses is consistent.

    Returns the feasible slope interval (the requested interval intersected
    with every pairwise pulse bound) and, when consistent, a concrete
    ``(a, b)`` witness on the lower slope boundary.
    """
    if slope_lo <= 0 or slope_hi < slope_lo:
        raise ValueError("slope interval must satisfy 0 < lo <= hi")

    reasons: list[dict] = []
    low = Fraction(slope_lo)
    high = Fraction(slope_hi)
    low_pair: tuple[Pulse, Pulse] | None = None
    high_pair: tuple[Pulse, Pulse] | None = None

    # Same tick: all arrival intervals must share a common point (independent
    # of a, so a violation here is fatal for every slope).
    by_tick: dict[int, list[Pulse]] = {}
    for pulse in pulses:
        by_tick.setdefault(pulse.tick, []).append(pulse)
    for tick, group in by_tick.items():
        max_lo = max(p.lo for p in group)
        min_hi = min(p.hi for p in group)
        if max_lo > min_hi:
            reasons.append(
                {
                    "reason": "same_tick_interval_empty",
                    "tick": tick,
                    "max_lo": frac_str(max_lo),
                    "min_hi": frac_str(min_hi),
                    "gap": frac_str(max_lo - min_hi),
                }
            )

    # Ordered pairs: lo_i - hi_j <= a*(tick_i - tick_j).
    for p in pulses:
        for q in pulses:
            d = p.tick - q.tick
            if d == 0:
                continue
            ratio = (p.lo - q.hi) / d
            if d > 0:
                if ratio > low:
                    low = ratio
                    low_pair = (p, q)
            else:
                if ratio < high:
                    high = ratio
                    high_pair = (p, q)

    if low > high:
        reason = {
            "reason": "slope_interval_empty",
            "lower": frac_str(low),
            "upper": frac_str(high),
            "gap": frac_str(low - high),
        }
        if low_pair is not None:
            reason["lower_pair"] = _pair_evidence(low_pair)
        if high_pair is not None:
            reason["upper_pair"] = _pair_evidence(high_pair)
        reasons.append(reason)

    if reasons:
        return Feasibility(
            consistent=False,
            requested_lo=Fraction(slope_lo),
            requested_hi=Fraction(slope_hi),
            feasible_lo=low,
            feasible_hi=high,
            witness_a=None,
            witness_b=None,
            reasons=tuple(reasons),
        )

    # Consistent: pick the lower slope boundary and the smallest feasible b.
    a = low
    if pulses:
        b = max(p.lo - a * p.tick for p in pulses)
    else:
        b = Fraction(0)
    return Feasibility(
        consistent=True,
        requested_lo=Fraction(slope_lo),
        requested_hi=Fraction(slope_hi),
        feasible_lo=low,
        feasible_hi=high,
        witness_a=a,
        witness_b=b,
        reasons=(),
    )


def _pair_evidence(pair: tuple[Pulse, Pulse]) -> dict:
    p, q = pair
    return {
        "high_pulse": {"pulse_id": p.pulse_id, "tick": p.tick, "lo": frac_str(p.lo), "hi": frac_str(p.hi)},
        "low_pulse": {"pulse_id": q.pulse_id, "tick": q.tick, "lo": frac_str(q.lo), "hi": frac_str(q.hi)},
    }


def verify_witness(feas: Feasibility, pulses: Sequence[Pulse]) -> None:
    """Assert that the stored witness satisfies every constraint (integrity)."""
    if not feas.consistent:
        raise ValueError("cannot verify a witness for an inconsistent calibration")
    a, b = feas.witness_a, feas.witness_b
    if a is None or b is None:
        raise ValueError("consistent calibration is missing a witness")
    if not (feas.requested_lo <= a <= feas.requested_hi):
        raise ValueError("witness slope outside the requested interval")
    for pulse in pulses:
        at = a * pulse.tick + b
        if not (pulse.lo <= at <= pulse.hi):
            raise ValueError(f"witness violates pulse {pulse.pulse_id!r}")


# ---------------------------------------------------------------------------
# achievable mapped time of a tick
# ---------------------------------------------------------------------------

def tick_value_bounds(
    feas: Feasibility,
    pulses: Sequence[Pulse],
    tick: int,
) -> tuple[Fraction | None, Fraction | None]:
    """Range of ``a*tick + b`` over every consistent ``(a, b)``.

    ``None`` on a side means unbounded (only possible when there are no
    pulses, hence no constraint on ``b``).  The range is a single closed
    interval because ``a*tick + L(a)`` is convex and ``a*tick + U(a)`` is
    concave over the feasible slope interval.
    """
    if not feas.consistent:
        raise ValueError("calibration is inconsistent")
    if not pulses:
        return None, None

    lo_a, hi_a = feas.feasible_lo, feas.feasible_hi
    candidates: set[Fraction] = {lo_a, hi_a}
    for i, p in enumerate(pulses):
        for q in pulses[i + 1:]:
            d = p.tick - q.tick
            if d == 0:
                continue
            for r in ((p.lo - q.lo) / d, (p.hi - q.hi) / d):
                if lo_a <= r <= hi_a:
                    candidates.add(r)

    v_min = min(max(p.lo + a * (tick - p.tick) for p in pulses) for a in candidates)
    v_max = max(min(p.hi + a * (tick - p.tick) for p in pulses) for a in candidates)
    return v_min, v_max


def mapped_moments(
    feas: Feasibility,
    pulses: Sequence[Pulse],
    ticks: Sequence[int],
) -> dict:
    """Per-tick achievable intervals and pairwise difference intervals.

    The *differences* between two moments in one segment depend only on the
    shared slope (``a*(tick_j - tick_i)``), never on the shared intercept, so
    they stay correlated no matter which calibration is chosen.  Recording
    them keeps the association between several moments of one segment.
    """
    per_tick = {}
    for tick in ticks:
        lo, hi = tick_value_bounds(feas, pulses, tick)
        per_tick[str(tick)] = {
            "tick": tick,
            "value_lo": None if lo is None else frac_str(lo),
            "value_hi": None if hi is None else frac_str(hi),
        }
    pairs = []
    for i, ti in enumerate(ticks):
        for tj in ticks[i + 1:]:
            d = tj - ti
            end_a = feas.feasible_lo * d
            end_b = feas.feasible_hi * d
            lo, hi = (end_a, end_b) if end_a <= end_b else (end_b, end_a)
            pairs.append(
                {
                    "from_tick": ti,
                    "to_tick": tj,
                    "difference_lo": frac_str(lo),
                    "difference_hi": frac_str(hi),
                }
            )
    return {"ticks": list(ticks), "per_tick": per_tick, "differences": pairs}


# ---------------------------------------------------------------------------
# exposure overlap
# ---------------------------------------------------------------------------

def overlap_for_delta(delta: Fraction, dt1: Fraction, dt2: Fraction) -> Fraction:
    """Overlap of ``[0, dt1]`` with ``[delta, delta+dt2]`` (translation free)."""
    left = delta if delta > 0 else Fraction(0)
    right = dt1 if dt1 < delta + dt2 else delta + dt2
    gap = right - left
    return gap if gap > 0 else Fraction(0)


def _overlap_breakpoints(dt1: Fraction, dt2: Fraction) -> list[Fraction]:
    # ov(delta) is piecewise linear in delta; these are all its kinks.
    return sorted({-dt2, dt1 - dt2, Fraction(0), dt1})


def _crossings(lo: Fraction, hi: Fraction, breaks: Sequence[Fraction], fn, threshold: Fraction) -> list[Fraction]:
    """All exact points in ``[lo, hi]`` where a piecewise-linear fn hits threshold."""
    inner = [b for b in breaks if lo < b < hi]
    points = sorted({lo, hi, *inner})
    out: set[Fraction] = set()
    for c0, c1 in zip(points, points[1:]):
        v0 = fn(c0)
        v1 = fn(c1)
        if v0 == v1:
            if v0 == threshold:
                out.add(c0)
            continue
        t = (threshold - v0) / (v1 - v0)
        if 0 <= t <= 1:
            out.add(c0 + t * (c1 - c0))
    return sorted(out)


def _classify(min_overlap: Fraction, max_overlap: Fraction, threshold: Fraction) -> str:
    if min_overlap >= threshold:
        return "ALL"
    if max_overlap < threshold:
        return "NONE"
    return "PARTIAL"


def _result(
    mode: str,
    min_overlap: Fraction,
    max_overlap: Fraction,
    threshold: Fraction,
    *,
    consistent: bool = True,
    reasons: Iterable[dict] = (),
    extra: dict | None = None,
) -> dict:
    classification = _classify(min_overlap, max_overlap, threshold) if consistent else "NONE"
    body = {
        "mode": mode,
        "classification": classification,
        "consistent": consistent,
        "threshold": threshold,
        "min_overlap": min_overlap if consistent else None,
        "max_overlap": max_overlap if consistent else None,
        "reasons": list(reasons),
        "witness": {},
    }
    if extra:
        body.update(extra)
    return body


def classify_shared(
    feas: Feasibility,
    pulses: Sequence[Pulse],
    s1: int,
    dt1: Fraction,
    s2: int,
    dt2: Fraction,
    threshold: Fraction,
) -> dict:
    """Classify overlap for two exposures that must share one calibration.

    With a shared ``(a, b)`` the intercept cancels and the start difference is
    ``a*(s2 - s1)``, so the overlap depends on ``a`` alone.  Because ``a > 0``
    the distance from perfect alignment grows with ``a``; the overlap is
    largest at ``feasible_lo`` and smallest at ``feasible_hi``.
    """
    if not feas.consistent:
        return _result(
            "shared", Fraction(0), Fraction(0), threshold,
            consistent=False, reasons=feas.reasons,
            extra={"feasible_slope": {"lo": feas.feasible_lo, "hi": feas.feasible_hi}},
        )

    factor = s2 - s1  # delta = a * factor
    def overlap_at(a: Fraction) -> Fraction:
        return overlap_for_delta(a * factor, dt1, dt2)

    a_lo, a_hi = feas.feasible_lo, feas.feasible_hi
    max_overlap = overlap_at(a_lo)
    min_overlap = overlap_at(a_hi)
    breaks = [b / factor for b in _overlap_breakpoints(dt1, dt2)] if factor != 0 else []
    crossings = _crossings(a_lo, a_hi, breaks, overlap_at, threshold) if factor != 0 else []

    witness = {
        "delta_factor": factor,
        "max_slope": a_lo,
        "max_overlap": max_overlap,
        "min_slope": a_hi,
        "min_overlap": min_overlap,
        "overlap_crossings": crossings,
    }
    result = _result(
        "shared", min_overlap, max_overlap, threshold,
        extra={
            "feasible_slope": {"lo": a_lo, "hi": a_hi},
            "boundary": {"slopes": crossings},
        },
    )
    result["witness"] = witness
    return result


def classify_independent(
    feas1: Feasibility,
    pulses1: Sequence[Pulse],
    s1: int,
    dt1: Fraction,
    feas2: Feasibility,
    pulses2: Sequence[Pulse],
    s2: int,
    dt2: Fraction,
    threshold: Fraction,
) -> dict:
    """Classify overlap for two exposures on independently-calibrated segments.

    Each exposure start ranges over its own interval, so the start difference
    ``delta`` ranges over one closed interval; the overlap depends on
    ``delta`` alone.  Overlap peaks at perfect alignment (``delta == 0``) and
    falls off linearly, so the extremes are at ``0`` and the far endpoint.
    """
    if not feas1.consistent or not feas2.consistent:
        reasons = list(feas1.reasons) + list(feas2.reasons)
        return _result(
            "independent", Fraction(0), Fraction(0), threshold,
            consistent=False, reasons=reasons,
            extra={
                "calibration_1": feas1.as_dict(),
                "calibration_2": feas2.as_dict(),
            },
        )

    v1lo, v1hi = tick_value_bounds(feas1, pulses1, s1)
    v2lo, v2hi = tick_value_bounds(feas2, pulses2, s2)
    # Convention: delta = start_2 - start_1, so overlap_for_delta(delta, dt1, dt2)
    # is [0,dt1] ∩ [delta, delta+dt2] after translating by -start_1.
    # Starts are independent, so:
    #   min delta = min start_2 - max start_1 = v2lo - v1hi
    #   max delta = max start_2 - min start_1 = v2hi - v1lo
    d_min = None if v2lo is None or v1hi is None else v2lo - v1hi
    d_max = None if v2hi is None or v1lo is None else v2hi - v1lo

    # Overlap is zero for |delta| >= max(dt), so an *unbounded* side can be
    # clipped to a finite sentinel without hiding a crossing.  A finite side is
    # used exactly.
    M = max(dt1, dt2) + 1
    lo = -M if d_min is None else d_min
    hi = M if d_max is None else d_max
    if lo > hi:
        raise ValueError("internal: empty start-difference range")

    def overlap_at(delta: Fraction) -> Fraction:
        return overlap_for_delta(delta, dt1, dt2)

    max_overlap = overlap_at(Fraction(0) if lo <= 0 <= hi else (lo if abs(lo) <= abs(hi) else hi))
    min_overlap = min(overlap_at(lo), overlap_at(hi))
    breaks = _overlap_breakpoints(dt1, dt2)
    crossings = _crossings(lo, hi, breaks, overlap_at, threshold)

    result = _result(
        "independent", min_overlap, max_overlap, threshold,
        extra={
            "start_range_1": {"lo": v1lo, "hi": v1hi},
            "start_range_2": {"lo": v2lo, "hi": v2hi},
            "delta_range": {"lo": d_min, "hi": d_max},
            "boundary": {"deltas": crossings},
            "calibration_1": {"feasible_slope": {"lo": feas1.feasible_lo, "hi": feas1.feasible_hi}},
            "calibration_2": {"feasible_slope": {"lo": feas2.feasible_lo, "hi": feas2.feasible_hi}},
        },
    )
    result["witness"] = {
        "delta_max_overlap": Fraction(0) if lo <= 0 <= hi else (lo if abs(lo) <= abs(hi) else hi),
        "max_overlap": max_overlap,
        "delta_min_overlap": lo if overlap_at(lo) <= overlap_at(hi) else hi,
        "min_overlap": min_overlap,
        "overlap_crossings": crossings,
    }
    return result


# ---------------------------------------------------------------------------
# concrete witnesses (a specific admissible calibration / exposure instance)
# ---------------------------------------------------------------------------

def _intercept_bounds(pulses: Sequence[Pulse], a: Fraction) -> tuple[Fraction, Fraction] | None:
    if not pulses:
        return None
    return (
        max(p.lo - a * p.tick for p in pulses),
        min(p.hi - a * p.tick for p in pulses),
    )


def _clamp_target(lo: Fraction | None, hi: Fraction | None, target: Fraction) -> Fraction:
    if lo is not None and target < lo:
        target = lo
    if hi is not None and target > hi:
        target = hi
    return target


def shared_witness(
    feas: Feasibility,
    pulses: Sequence[Pulse],
    s1: int,
    dt1: Fraction,
    s2: int,
    dt2: Fraction,
    threshold: Fraction,
) -> dict:
    """A concrete shared calibration reaching the largest possible overlap."""
    a = feas.feasible_lo
    bounds = _intercept_bounds(pulses, a)
    b = bounds[0] if bounds is not None else Fraction(0)
    start1 = a * s1 + b
    start2 = a * s2 + b
    delta = start2 - start1
    overlap = overlap_for_delta(delta, dt1, dt2)
    return {
        "calibration": {
            "a": frac_str(a),
            "b": frac_str(b),
            "pulse_bounds": None
            if bounds is None
            else {"lo": frac_str(bounds[0]), "hi": frac_str(bounds[1])},
        },
        "exposure_1": {"start_tick": s1, "start": frac_str(start1), "duration": frac_str(dt1)},
        "exposure_2": {"start_tick": s2, "start": frac_str(start2), "duration": frac_str(dt2)},
        "start_difference": frac_str(delta),
        "overlap": frac_str(overlap),
        "meets_threshold": overlap >= threshold,
    }


def _solve_start(
    feas: Feasibility,
    pulses: Sequence[Pulse],
    s: int,
    start: Fraction,
) -> tuple[Fraction, Fraction] | None:
    """Find ``(a, b)`` in the feasible band with ``a*s + b == start``.

    Equivalent to finding a slope ``a`` such that for every pulse
    ``lo_i <= start + a*(tick_i - s) <= hi_i``.  Each pulse bounds ``a`` from
    one or both sides, so this is an interval intersection in one variable and
    is decided exactly.  Returns ``None`` when ``start`` is out of reach.
    """
    lo = feas.feasible_lo
    hi = feas.feasible_hi
    for pulse in pulses:
        d = pulse.tick - s
        if d == 0:
            if not (pulse.lo <= start <= pulse.hi):
                return None
            continue
        bound_lo = (pulse.lo - start) / d
        bound_hi = (pulse.hi - start) / d
        if d < 0:
            bound_lo, bound_hi = bound_hi, bound_lo
        if bound_lo > lo:
            lo = bound_lo
        if bound_hi < hi:
            hi = bound_hi
        if lo > hi:
            return None
    a = lo
    b = start - a * s
    # Guard against an unbounded (pulse-free) product: any b is admissible.
    for pulse in pulses:
        at = a * pulse.tick + b
        if not (pulse.lo <= at <= pulse.hi):
            raise AssertionError("internal: witness start solution violates a pulse")
    return a, b


def independent_witness(
    feas1: Feasibility,
    pulses1: Sequence[Pulse],
    s1: int,
    dt1: Fraction,
    feas2: Feasibility,
    pulses2: Sequence[Pulse],
    s2: int,
    dt2: Fraction,
    threshold: Fraction,
) -> dict:
    """A concrete pair of independent calibrations reaching the largest overlap.

    Chooses a start difference as close to perfect alignment as the two start
    ranges allow, then realises each exposure start with an exact 1-D solve in
    its own slope band (not merely at slope kinks).  Each returned calibration
    satisfies its pulses by construction.
    """
    v1lo, v1hi = tick_value_bounds(feas1, pulses1, s1)
    v2lo, v2hi = tick_value_bounds(feas2, pulses2, s2)

    # Convention delta = v2 - v1, matching classify_independent.
    d_lo = None if v2lo is None or v1hi is None else v2lo - v1hi
    d_hi = None if v2hi is None or v1lo is None else v2hi - v1lo
    target = _clamp_target(d_lo, d_hi, Fraction(0))

    # Need v1 in range1 and v2 = v1 + target in range2.
    low = v1lo if v1lo is not None else (None if v2lo is None else v2lo - target)
    if v2lo is not None and (low is None or v2lo - target > low):
        low = v2lo - target
    high = v1hi if v1hi is not None else (None if v2hi is None else v2hi - target)
    if v2hi is not None and (high is None or v2hi - target < high):
        high = v2hi - target
    if low is not None and high is not None:
        if low > high:
            # Numerical-free but degenerate: clamp target to the nearest end.
            low = high
        v1 = (low + high) / 2
    elif low is not None:
        v1 = low
    elif high is not None:
        v1 = high
    else:
        v1 = Fraction(0)
    v2 = v1 + target

    sol1 = _solve_start(feas1, pulses1, s1, v1)
    sol2 = _solve_start(feas2, pulses2, s2, v2)
    if sol1 is None or sol2 is None:
        # Fall back to the two boundary starts (always realisable).
        v1 = v1lo if v1lo is not None else (v2lo - target if v2lo is not None else Fraction(0))
        v2 = v2lo if v2lo is not None else Fraction(0)
        sol1 = _solve_start(feas1, pulses1, s1, v1)
        sol2 = _solve_start(feas2, pulses2, s2, v2)
    if sol1 is None or sol2 is None:
        raise AssertionError("internal: could not realise an admissible witness")

    a1, b1 = sol1
    a2, b2 = sol2
    delta = v2 - v1
    overlap = overlap_for_delta(delta, dt1, dt2)
    return {
        "calibration_1": {"a": frac_str(a1), "b": frac_str(b1), "exposure_start": frac_str(v1)},
        "calibration_2": {"a": frac_str(a2), "b": frac_str(b2), "exposure_start": frac_str(v2)},
        "exposure_1": {"start_tick": s1, "duration": frac_str(dt1)},
        "exposure_2": {"start_tick": s2, "duration": frac_str(dt2)},
        "start_difference": frac_str(delta),
        "overlap": frac_str(overlap),
        "meets_threshold": overlap >= threshold,
    }
