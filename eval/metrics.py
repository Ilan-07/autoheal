"""Alignment-free scoring.

Records can be reordered, duplicated or dropped by a mutation, so we score each
field as a *multiset* against ground truth. Precision falls when the extractor
emits values that were never on the page -- which is exactly what a decoy does,
and exactly what a fill-rate metric would miss.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any


def _multiset(rows: list[dict], key: str) -> Counter:
    return Counter(_norm(r.get(key)) for r in rows if r.get(key) is not None)


def _norm(v: Any) -> Any:
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        return " ".join(v.split())
    return v


def prf(pred: Counter, truth: Counter) -> tuple[float, float, float]:
    overlap = sum((pred & truth).values())
    p = overlap / sum(pred.values()) if pred else 0.0
    r = overlap / sum(truth.values()) if truth else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f1


@dataclass
class Score:
    fields: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    macro_f1: float = 0.0
    record_em: float = 0.0
    fill: dict[str, float] = field(default_factory=dict)
    n_pred: int = 0
    n_truth: int = 0

    @property
    def silent(self) -> bool:
        """High fill, low accuracy: the failure mode the whole project targets."""
        avg_fill = sum(self.fill.values()) / len(self.fill) if self.fill else 0.0
        return avg_fill >= 0.9 and self.macro_f1 < 0.9


def score(pred_rows: list[dict], truth_rows: list[dict], field_names: list[str]) -> Score:
    s = Score(n_pred=len(pred_rows), n_truth=len(truth_rows))
    for f in field_names:
        s.fields[f] = prf(_multiset(pred_rows, f), _multiset(truth_rows, f))
        s.fill[f] = (sum(1 for r in pred_rows if r.get(f) is not None) / len(pred_rows)) if pred_rows else 0.0
    s.macro_f1 = sum(v[2] for v in s.fields.values()) / len(field_names) if field_names else 0.0

    def tup(r: dict) -> tuple:
        return tuple(_norm(r.get(f)) for f in field_names)

    pt, tt = Counter(map(tup, pred_rows)), Counter(map(tup, truth_rows))
    s.record_em = sum((pt & tt).values()) / len(truth_rows) if truth_rows else 0.0
    return s
