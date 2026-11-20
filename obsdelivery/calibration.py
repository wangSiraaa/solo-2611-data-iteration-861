"""Segment calibration products: pulse admission, consistency, read-back.

A *segment* is one station's acquisition span.  Its calibration is an affine
map ``T = a*tick + b`` with ``a`` in the segment's positive slope interval and
``b`` constrained by time-sync pulses.  Pulses are admitted with exactly the
same revision rules as observations (highest revision wins, same-revision
conflicting bits reject the whole batch, lower revisions are accepted but
ignored).

``publish_calibration_product`` snapshots the *effective* pulses, solves the
constraints exactly, and either:

* publishes an immutable product carrying the feasible slope interval, an
  exact ``(a, b)`` witness, and the pairwise association between the segment's
  moments (so several moments stay correlated), or
* refuses an inconsistent calibration, recording an attempt and touching no
  product row.

Refusing a new product never disturbs products already stored: an older,
still-consistent product reads back unchanged after the database is reopened.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable, Sequence

from . import semantics
from .calibration_algo import (
    Feasibility,
    Pulse,
    evaluate_calibration,
    mapped_moments,
    verify_witness,
)
from .canonical import canonical_json, digest, frac_str, parse_fraction
from .errors import (
    BatchRejected,
    CalibrationConflict,
    CalibrationNotFound,
    CalibrationRejected,
    IntegrityError,
    ValidationError,
)
from .storage import Store, utcnow


def _require_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string, got {value!r}")
    return value


def _require_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(f"revision must be a positive integer, got {value!r}")
    return value


def _require_tick(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"tick must be an integer, got {value!r}")
    return value


def _pulse_value(bits: Any) -> tuple[str, Fraction]:
    """Interpret a finite sync-reference bit pattern exactly."""
    try:
        decoded = semantics.decode(semantics.parse_bits(bits))
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if not decoded.finite:
        raise ValidationError(
            f"sync-reference bits must be finite, got {decoded.bits} ({decoded.kind})"
        )
    return decoded.bits, decoded.value


@dataclass(frozen=True)
class _PulseEntry:
    pulse_id: str
    revision: int
    tick: int
    bits: str
    halfwidth: Fraction


class CalibrationService:
    """Segment calibration over one SQLite store (shares the semantics store)."""

    def __init__(self, store: Store):
        self.store = store

    # -- segments -----------------------------------------------------------
    def register_segment(
        self,
        segment_id: str,
        station_id: str,
        slope_lo: Fraction | str | int,
        slope_hi: Fraction | str | int,
    ) -> dict:
        """Create (or replay) an immutable segment with its positive slope band."""
        _require_id(segment_id, "segment_id")
        _require_id(station_id, "station_id")
        try:
            lo = parse_fraction(slope_lo, field="slope_lo")
            hi = parse_fraction(slope_hi, field="slope_hi")
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        if lo <= 0:
            raise ValidationError("slope_lo must be strictly positive")
        if hi < lo:
            raise ValidationError("slope_hi must be >= slope_lo")

        body = {
            "segment_id": segment_id,
            "station_id": station_id,
            "slope_lo": lo,
            "slope_hi": hi,
        }
        content_digest = digest(body)
        existing = self.store.conn.execute(
            "SELECT * FROM calibration_segments WHERE segment_id=?", (segment_id,)
        ).fetchone()
        if existing is not None:
            if existing["content_digest"] != content_digest:
                raise CalibrationConflict(
                    f"segment {segment_id!r} exists with different station/slope"
                )
            return self._segment_view(existing)
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO calibration_segments "
                "(segment_id, station_id, slope_lo, slope_hi, content_digest, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (segment_id, station_id, frac_str(lo), frac_str(hi), content_digest, utcnow()),
            )
        row = self.store.conn.execute(
            "SELECT * FROM calibration_segments WHERE segment_id=?", (segment_id,)
        ).fetchone()
        return self._segment_view(row)

    def _segment_row(self, segment_id: str):
        row = self.store.conn.execute(
            "SELECT * FROM calibration_segments WHERE segment_id=?", (segment_id,)
        ).fetchone()
        if row is None:
            raise CalibrationNotFound(segment_id)
        return row

    def _segment_view(self, row) -> dict:
        return {
            "segment_id": row["segment_id"],
            "station_id": row["station_id"],
            "slope_lo": row["slope_lo"],
            "slope_hi": row["slope_hi"],
            "content_digest": row["content_digest"],
        }

    def segment(self, segment_id: str) -> dict:
        return self._segment_view(self._segment_row(segment_id))

    # -- pulse admission ----------------------------------------------------
    def _normalise_pulses(self, records: Iterable[dict]) -> list[_PulseEntry]:
        if isinstance(records, (str, bytes)) or not isinstance(records, Iterable):
            raise ValidationError("pulses must be an iterable of objects")
        entries: dict[tuple[str, int], _PulseEntry] = {}
        for record in records:
            if not isinstance(record, dict):
                raise ValidationError(f"pulse record is not an object: {record!r}")
            pulse_id = _require_id(record.get("pulse_id"), "pulse_id")
            revision = _require_revision(record.get("revision"))
            tick = _require_tick(record.get("tick"))
            bits, _value = _pulse_value(record.get("bits"))
            try:
                halfwidth = parse_fraction(record.get("halfwidth"), field="halfwidth")
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            if halfwidth < 0:
                raise ValidationError(f"halfwidth for {pulse_id} must be non-negative")
            key = (pulse_id, revision)
            entry = _PulseEntry(pulse_id, revision, tick, bits, halfwidth)
            if key in entries:
                prior = entries[key]
                if (prior.tick, prior.bits, prior.halfwidth) != (tick, bits, halfwidth):
                    raise BatchRejected(
                        [
                            {
                                "reason": "duplicate_pulse_revision_conflict",
                                "pulse_id": pulse_id,
                                "revision": revision,
                            }
                        ]
                    )
                continue
            entries[key] = entry
        if not entries:
            raise ValidationError("pulse batch must contain at least one record")
        return list(entries.values())

    def effective_pulse_revision(self, segment_id: str, pulse_id: str) -> int | None:
        row = self.store.conn.execute(
            "SELECT MAX(revision) AS rev FROM calibration_pulses "
            "WHERE segment_id=? AND pulse_id=?",
            (segment_id, pulse_id),
        ).fetchone()
        return None if row is None or row["rev"] is None else row["rev"]

    def effective_pulses(self, segment_id: str) -> list[Pulse]:
        rows = self.store.conn.execute(
            "SELECT pulse_id, revision, tick, bits, halfwidth, lo, hi "
            "FROM calibration_pulses p WHERE segment_id=? AND revision = "
            "(SELECT MAX(revision) FROM calibration_pulses q "
            " WHERE q.segment_id=p.segment_id AND q.pulse_id=p.pulse_id) "
            "ORDER BY pulse_id",
            (segment_id,),
        ).fetchall()
        return [
            Pulse(
                pulse_id=row["pulse_id"],
                tick=row["tick"],
                lo=parse_fraction(row["lo"], field="lo"),
                hi=parse_fraction(row["hi"], field="hi"),
                revision=row["revision"],
                bits=row["bits"],
            )
            for row in rows
        ]

    def admit_pulses(self, segment_id: str, records: Iterable[dict]) -> dict:
        """Admit pulse revisions atomically (highest revision wins)."""
        self._segment_row(segment_id)  # segment must exist
        entries = self._normalise_pulses(records)
        ordered = sorted(entries, key=lambda e: (e.pulse_id, e.revision))
        batch_body = [
            {
                "pulse_id": e.pulse_id,
                "revision": e.revision,
                "tick": e.tick,
                "bits": e.bits,
                "halfwidth": frac_str(e.halfwidth),
            }
            for e in ordered
        ]
        batch_digest = digest({"segment_id": segment_id, "pulses": batch_body})
        existing = self.store.conn.execute(
            "SELECT * FROM calibration_pulse_admissions WHERE batch_digest=?",
            (batch_digest,),
        ).fetchone()
        if existing is not None:
            return self._admission_view(existing, created=False)

        conflicts: list[dict] = []
        plans: list[dict] = []
        for entry in ordered:
            same = self.store.conn.execute(
                "SELECT tick, bits, halfwidth FROM calibration_pulses "
                "WHERE segment_id=? AND pulse_id=? AND revision=?",
                (segment_id, entry.pulse_id, entry.revision),
            ).fetchone()
            offered = (entry.tick, entry.bits, frac_str(entry.halfwidth))
            if same is not None and (same["tick"], same["bits"], same["halfwidth"]) != offered:
                conflicts.append(
                    {
                        "reason": "pulse_revision_conflict",
                        "pulse_id": entry.pulse_id,
                        "revision": entry.revision,
                        "stored": [same["tick"], same["bits"], same["halfwidth"]],
                        "offered": list(offered),
                    }
                )
                continue
            current = self.effective_pulse_revision(segment_id, entry.pulse_id)
            if current is None or entry.revision > current:
                disposition = "new"
            elif entry.revision == current:
                disposition = "replayed"
            else:
                disposition = "ignored_older"
            plans.append({"entry": entry, "disposition": disposition})

        canonical = {"pulses": batch_body, "conflicts": conflicts}
        with self.store.transaction() as conn:
            if conflicts:
                cur = conn.execute(
                    "INSERT INTO calibration_pulse_admissions "
                    "(segment_id, batch_digest, accepted, outcome, canonical, created_at) "
                    "VALUES (?, ?, 0, 'rejected', ?, ?)",
                    (segment_id, batch_digest, canonical_json(canonical), utcnow()),
                )
                admission_id = cur.lastrowid
                for entry in ordered:
                    self._insert_pulse_member(conn, admission_id, entry, "rejected")
            else:
                cur = conn.execute(
                    "INSERT INTO calibration_pulse_admissions "
                    "(segment_id, batch_digest, accepted, outcome, canonical, created_at) "
                    "VALUES (?, ?, ?, 'accepted', ?, ?)",
                    (segment_id, batch_digest, len(plans), canonical_json(canonical), utcnow()),
                )
                admission_id = cur.lastrowid
                for plan in plans:
                    entry = plan["entry"]
                    self._insert_pulse_member(conn, admission_id, entry, plan["disposition"])
                    if plan["disposition"] == "new":
                        value = _pulse_value(entry.bits)[1]
                        conn.execute(
                            "INSERT INTO calibration_pulses "
                            "(segment_id, pulse_id, revision, tick, bits, halfwidth, "
                            " lo, hi, admission_id) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                segment_id,
                                entry.pulse_id,
                                entry.revision,
                                entry.tick,
                                entry.bits,
                                frac_str(entry.halfwidth),
                                frac_str(value - entry.halfwidth),
                                frac_str(value + entry.halfwidth),
                                admission_id,
                            ),
                        )
        row = self.store.conn.execute(
            "SELECT * FROM calibration_pulse_admissions WHERE admission_id=?",
            (admission_id,),
        ).fetchone()
        result = self._admission_view(row, created=True)
        if conflicts:
            raise CalibrationBatchRejected(result)
        return result

    @staticmethod
    def _insert_pulse_member(conn, admission_id: int, entry: _PulseEntry, disposition: str) -> None:
        conn.execute(
            "INSERT INTO calibration_pulse_batch_member "
            "(admission_id, pulse_id, revision, tick, bits, halfwidth, disposition) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                admission_id,
                entry.pulse_id,
                entry.revision,
                entry.tick,
                entry.bits,
                frac_str(entry.halfwidth),
                disposition,
            ),
        )

    def _admission_view(self, row, created: bool) -> dict:
        members = self.store.conn.execute(
            "SELECT pulse_id, revision, tick, bits, halfwidth, disposition "
            "FROM calibration_pulse_batch_member WHERE admission_id=? "
            "ORDER BY pulse_id, revision",
            (row["admission_id"],),
        ).fetchall()
        return {
            "admission_id": row["admission_id"],
            "segment_id": row["segment_id"],
            "batch_digest": row["batch_digest"],
            "outcome": row["outcome"],
            "accepted_count": row["accepted"],
            "created": created,
            "members": [dict(m) for m in members],
        }

    # -- calibration products ----------------------------------------------
    def solve(self, segment_id: str) -> Feasibility:
        """Score the segment's current pulses without publishing anything."""
        segment = self._segment_row(segment_id)
        pulses = self.effective_pulses(segment_id)
        return evaluate_calibration(
            parse_fraction(segment["slope_lo"], field="slope_lo"),
            parse_fraction(segment["slope_hi"], field="slope_hi"),
            pulses,
        )

    def publish_calibration_product(
        self,
        product_id: str,
        segment_id: str,
        ticks: Sequence[int] | None = None,
    ) -> dict:
        """Publish an immutable calibration product, or refuse inconsistency."""
        _require_id(product_id, "product_id")
        segment = self._segment_row(segment_id)
        slope_lo = parse_fraction(segment["slope_lo"], field="slope_lo")
        slope_hi = parse_fraction(segment["slope_hi"], field="slope_hi")
        pulses = self.effective_pulses(segment_id)

        moment_ticks = self._normalise_ticks(ticks, pulses)
        feas = evaluate_calibration(slope_lo, slope_hi, pulses)
        pulses_body = [p.canonical() for p in pulses]
        moments = mapped_moments(feas, pulses, moment_ticks) if feas.consistent else None

        body = {
            "kind": "calibration_product",
            "product_id": product_id,
            "segment_id": segment_id,
            "station_id": segment["station_id"],
            "slope": {"lo": slope_lo, "hi": slope_hi},
            "pulses": pulses_body,
            "feasible": feas.as_dict(),
            "moments": moments,
        }

        if not feas.consistent:
            attempt_id = self._record_attempt(
                product_id, segment_id, "rejected", feas.reasons
            )
            raise CalibrationRejected(
                product_id, list(feas.reasons), attempt_id=attempt_id
            )

        verify_witness(feas, pulses)
        content_digest = digest(body)

        existing = self.store.conn.execute(
            "SELECT * FROM calibration_products WHERE product_id=?", (product_id,)
        ).fetchone()
        if existing is not None:
            if existing["content_digest"] != content_digest:
                raise CalibrationConflict(
                    f"calibration product {product_id!r} exists with different content"
                )
            return self._product_view(existing, created=False)

        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO calibration_products "
                "(product_id, segment_id, station_id, slope_lo, slope_hi, "
                " feasible_lo, feasible_hi, witness_a, witness_b, ticks_json, "
                " moments_json, content_digest, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    product_id,
                    segment_id,
                    segment["station_id"],
                    frac_str(slope_lo),
                    frac_str(slope_hi),
                    frac_str(feas.feasible_lo),
                    frac_str(feas.feasible_hi),
                    frac_str(feas.witness_a),
                    frac_str(feas.witness_b),
                    canonical_json(list(moment_ticks)),
                    canonical_json(moments),
                    content_digest,
                    utcnow(),
                ),
            )
            for pulse in pulses:
                conn.execute(
                    "INSERT INTO calibration_product_pulse "
                    "(product_id, pulse_id, revision, tick, bits, halfwidth, lo, hi) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        product_id,
                        pulse.pulse_id,
                        pulse.revision,
                        pulse.tick,
                        pulse.bits,
                        frac_str(Fraction(pulse.hi - pulse.lo, 2)),
                        frac_str(pulse.lo),
                        frac_str(pulse.hi),
                    ),
                )
            conn.execute(
                "INSERT INTO calibration_attempts "
                "(product_id, segment_id, status, reasons_json, created_at) "
                "VALUES (?, ?, 'published', ?, ?)",
                (product_id, segment_id, canonical_json([]), utcnow()),
            )
        row = self.store.conn.execute(
            "SELECT * FROM calibration_products WHERE product_id=?", (product_id,)
        ).fetchone()
        return self._product_view(row, created=True)

    @staticmethod
    def _normalise_ticks(ticks: Sequence[int] | None, pulses: Sequence[Pulse]) -> list[int]:
        if ticks is None:
            return sorted({p.tick for p in pulses})
        if isinstance(ticks, (str, bytes)) or not isinstance(ticks, Sequence):
            raise ValidationError("ticks must be a sequence of integers")
        out = [_require_tick(t) for t in ticks]
        if not out:
            raise ValidationError("ticks must be non-empty (or None to use pulse ticks)")
        return sorted(set(out))

    def _record_attempt(self, product_id: str, segment_id: str, status: str, reasons) -> int:
        with self.store.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO calibration_attempts "
                "(product_id, segment_id, status, reasons_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (product_id, segment_id, status, canonical_json(list(reasons)), utcnow()),
            )
            return cur.lastrowid

    def get_calibration_product(self, product_id: str) -> dict:
        row = self.store.conn.execute(
            "SELECT * FROM calibration_products WHERE product_id=?", (product_id,)
        ).fetchone()
        if row is None:
            raise CalibrationNotFound(product_id)
        return self._product_view(row, created=False)

    #: Alias that reads like the semantic-product accessor.
    get_product = get_calibration_product

    def _product_pulses(self, product_id: str) -> list[Pulse]:
        rows = self.store.conn.execute(
            "SELECT pulse_id, revision, tick, bits, halfwidth, lo, hi "
            "FROM calibration_product_pulse WHERE product_id=? ORDER BY pulse_id",
            (product_id,),
        ).fetchall()
        return [
            Pulse(
                pulse_id=row["pulse_id"],
                tick=row["tick"],
                lo=parse_fraction(row["lo"], field="lo"),
                hi=parse_fraction(row["hi"], field="hi"),
                revision=row["revision"],
                bits=row["bits"],
            )
            for row in rows
        ]

    def _product_view(self, row, created: bool) -> dict:
        """Reload the stored pulses and re-derive everything from them."""
        pulses = self._product_pulses(row["product_id"])
        slope_lo = parse_fraction(row["slope_lo"], field="slope_lo")
        slope_hi = parse_fraction(row["slope_hi"], field="slope_hi")
        ticks = json.loads(row["ticks_json"])
        moments = json.loads(row["moments_json"])
        feas = evaluate_calibration(slope_lo, slope_hi, pulses)
        if not feas.consistent:
            raise IntegrityError(
                f"calibration product {row['product_id']!r} stores an inconsistent witness"
            )
        verify_witness(feas, pulses)

        body = {
            "kind": "calibration_product",
            "product_id": row["product_id"],
            "segment_id": row["segment_id"],
            "station_id": row["station_id"],
            "slope": {"lo": slope_lo, "hi": slope_hi},
            "pulses": [p.canonical() for p in pulses],
            "feasible": feas.as_dict(),
            "moments": moments,
        }
        actual = digest(body)
        if actual != row["content_digest"]:
            raise IntegrityError(
                f"calibration product {row['product_id']!r} digest mismatch "
                f"({actual} != {row['content_digest']})"
            )
        if mapped_moments(feas, pulses, ticks) != moments:
            raise IntegrityError(
                f"calibration product {row['product_id']!r}: moment association differs"
            )
        return {
            "kind": "calibration_product",
            "product_id": row["product_id"],
            "segment_id": row["segment_id"],
            "station_id": row["station_id"],
            "slope": {"lo": frac_str(slope_lo), "hi": frac_str(slope_hi)},
            "feasible_slope": {
                "lo": frac_str(feas.feasible_lo),
                "hi": frac_str(feas.feasible_hi),
            },
            "witness": {"a": frac_str(feas.witness_a), "b": frac_str(feas.witness_b)},
            "pulses": [p.canonical() for p in pulses],
            "ticks": ticks,
            "moments": moments,
            "content_digest": row["content_digest"],
            "created": created,
        }

    def read_calibration_product(self, product_id: str) -> dict:
        return self.get_calibration_product(product_id)

    def attempts(self, product_id: str) -> list[dict]:
        rows = self.store.conn.execute(
            "SELECT attempt_id, product_id, segment_id, status, reasons_json, created_at "
            "FROM calibration_attempts WHERE product_id=? ORDER BY attempt_id",
            (product_id,),
        ).fetchall()
        return [
            {
                "attempt_id": row["attempt_id"],
                "product_id": row["product_id"],
                "segment_id": row["segment_id"],
                "status": row["status"],
                "reasons": json.loads(row["reasons_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]


class CalibrationBatchRejected(BatchRejected):
    """Internal: carries the audited rejected pulse admission result."""

    def __init__(self, result: dict):
        self.result = result
        super().__init__(
            [
                {
                    "reason": "pulse_revision_conflict",
                    "admission_id": result["admission_id"],
                    "members": result["members"],
                }
            ]
        )
