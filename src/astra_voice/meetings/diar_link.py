"""Deterministic speaker clustering/linking; voice features never leave this process.

Imports need neither numpy nor a model runtime. Centroid instances are deliberately
ordinary opaque objects, not wire records or dataclasses suitable for asdict().
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any, NamedTuple

DIAR_ALGORITHM = "sherpa-pyannote3+3dspeaker-campplus+adaptive-centroid-support-v2"
MAX_BLOCKS = 12
MAX_SEGMENTS = 28800
MAX_LOCAL_SPEAKERS = 128  # bounded oversegmentation before the adaptive merge
MAX_SPEAKERS = 64  # cumulative, including provisional identities subsequently merged


class Profile(NamedTuple):
    """Three calibrated constants; none depends on a target speaker count.

    seed_threshold oversegments sherpa in ONE inference pass. merge_ceiling
    bounds ordinary centroid clustering in cosine distance (1-cos); the largest
    absolute gap inside [0, ceiling] chooses the data-derived meeting scale.
    Including both endpoints permits a homogeneous block to have one speaker.

    min_support_s is the speech-duration scale for a one-block voice estimate:
    very brief clips have less phonetic coverage and noisier centroids. Below
    this scale, final consolidation linearly relaxes the admissible distance
    from merge_ceiling toward orthogonality (distance 1). It uses only the
    nearest supported voice, without contaminating that voice's centroid.
    Evidence in two distinct blocks independently supports even a brief identity;
    such voices retain the ordinary meeting cut, but their short estimates do
    not become targets for relaxed merging. Thus a singleton may lose its
    separate identity for LESS THAN min_support_s seconds, but a returning
    distant guest is preserved. This is an uncertainty rule, not a minimum
    speaking time or a prescribed number of participants.
    """

    seed_threshold: float
    merge_ceiling: float
    min_support_s: float = 8.0


DIAR_PROFILES = {"3dspeaker-campplus": Profile(0.5, 0.45, 8.0)}


class LimitError(ValueError):
    """Meeting-wide capacity exhausted; caller must terminate the job."""


class Centroid:
    __slots__ = ("vector", "duration")

    def __init__(self, vector: Any, duration: float) -> None:
        import numpy as np

        values = np.asarray(vector, dtype=np.float64)
        if values.ndim != 1 or not 1 <= values.size <= 4096 or not np.isfinite(values).all():
            raise ValueError("diar-vector-invalid")
        norm = float(np.linalg.norm(values))
        if not math.isfinite(norm) or norm <= 0 or not math.isfinite(duration) or duration <= 0:
            raise ValueError("diar-vector-invalid")
        self.vector = values / norm
        self.duration = float(duration)

    def merged(self, other: Centroid) -> Centroid:
        if self.vector.shape != other.vector.shape:
            raise ValueError("diar-vector-dimension")
        combined = object.__new__(Centroid)
        combined.duration = self.duration + other.duration
        combined.vector = (
            self.vector * self.duration + other.vector * other.duration
        ) / combined.duration
        return combined


def distance(a: Centroid, b: Centroid) -> float:
    if a.vector.shape != b.vector.shape:
        raise ValueError("diar-vector-dimension")
    denominator = float((a.vector @ a.vector) ** 0.5) * float((b.vector @ b.vector) ** 0.5)
    if denominator <= 0:
        return 2.0  # cancellation of antipodal means is no evidence of identity
    return max(0.0, min(2.0, 1.0 - float(a.vector @ b.vector) / denominator))


def hierarchy(centroids: Sequence[Centroid]) -> list[tuple[float, int, int]]:
    """Duration-weighted centroid linkage, stable ties by original first index."""
    if len(centroids) > MAX_LOCAL_SPEAKERS + MAX_SPEAKERS:
        raise LimitError("limit")
    live = dict(enumerate(centroids))
    steps = []
    height = 0.0
    while len(live) > 1:
        value, a, b = min((distance(live[a], live[b]), a, b) for a in live for b in live if a < b)
        height = max(height, value)  # centroid linkage can have inversions
        steps.append((height, a, b))
        live[a] = live[a].merged(live.pop(b))
    return steps


def choose_cut(steps: Sequence[tuple[float, int, int]], ceiling: float) -> float:
    """Midpoint of the largest absolute gap in [0, ceiling]; earliest tie wins.

    Zero and the profile ceiling bound the scale. Including the tail gap lets
    homogeneous speech become one speaker. Relative-gap selection was rejected
    by calibration: tiny distances inside a dense cloud caused oversegmentation.
    """
    if not math.isfinite(ceiling) or not 0 < ceiling <= 2:
        raise ValueError("diar-ceiling-invalid")
    values = [0.0, *(d for d, _, _ in steps if d < ceiling), ceiling]
    _, low, high = max(
        ((b - a, -a, b) for a, b in zip(values, values[1:], strict=False)),
        key=lambda row: (row[0], row[1]),
    )
    return (-low + high) / 2


def merge_at_cut(centroids: Sequence[Centroid], cut: float) -> list[int]:
    steps = hierarchy(centroids)
    groups = list(range(len(centroids)))
    for value, a, b in steps:
        if value > cut:
            break
        groups = [a if g == b else g for g in groups]
    return groups


def cluster(centroids: Sequence[Centroid], ceiling: float) -> tuple[list[int], float]:
    cut = choose_cut(hierarchy(centroids), ceiling)
    return merge_at_cut(centroids, cut), cut


def maximize_assignment(weights: Sequence[Sequence[float]]) -> list[tuple[int, int]]:
    """Exact rectangular Hungarian assignment in O(n^3), with stable tie order.

    Zero padding models unmatched rows; no scipy dependency or greedy fallback.
    """
    rows = len(weights)
    cols = len(weights[0]) if rows else 0
    if any(len(row) != cols or any(not math.isfinite(v) for v in row) for row in weights):
        raise ValueError("diar-assignment-invalid")
    size = max(rows, cols)
    if not rows or not cols:
        return []
    u, v = [0.0] * (size + 1), [0.0] * (size + 1)
    p, way = [0] * (size + 1), [0] * (size + 1)
    for i in range(1, size + 1):
        p[0] = i
        j0 = 0
        best, used = [math.inf] * (size + 1), [False] * (size + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], math.inf, 0
            for j in range(1, size + 1):
                if used[j]:
                    continue
                weight = weights[i0 - 1][j - 1] if i0 <= rows and j <= cols else 0.0
                current = -weight - u[i0] - v[j]
                if current < best[j]:
                    best[j], way[j] = current, j0
                if best[j] < delta:
                    delta, j1 = best[j], j
            for j in range(size + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    best[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    return sorted((p[j] - 1, j - 1) for j in range(1, size + 1) if p[j] <= rows and j <= cols)


class SpeakerLinker:
    """All historical speakers are candidates, including those silent for blocks."""

    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.globals: dict[int, Centroid] = {}
        self.blocks = 0
        self.segments = 0
        self.end_ms = 0
        self.cut = profile.merge_ceiling / 2
        self.cut_weight = 0.0
        self.observations: dict[int, set[int]] = {}

    def link(
        self, turns: Sequence[tuple[int, int, int]], centroids: dict[int, Centroid], offset_ms: int
    ) -> list[list[Any]]:
        if (
            self.blocks >= MAX_BLOCKS
            or self.segments + len(turns) > MAX_SEGMENTS
            or len(centroids) > MAX_LOCAL_SPEAKERS
        ):
            raise LimitError("limit")
        ordered = sorted(turns, key=lambda row: (row[0], row[1], row[2]))
        locals_ = list(dict.fromkeys(row[2] for row in ordered))
        if (
            set(locals_) != set(centroids)
            or offset_ms < self.end_ms
            or any(type(a) is not int or type(b) is not int or not 0 <= a < b for a, b, _ in turns)
        ):
            raise ValueError("diar-block-invalid")
        groups, local_cut = cluster([centroids[k] for k in locals_], self.profile.merge_ceiling)
        # Once locals have been merged, their internal scatter is lost. Re-fitting
        # an elbow to only these global means can merge distinct close voices.
        # Carry the data-derived local cut (duration-weighted over all blocks)
        # through matching AND consolidation instead of inventing a new scale.
        weight = sum(c.duration for c in centroids.values())
        cut_weight = self.cut_weight + weight
        cut = (
            (self.cut * self.cut_weight + local_cut * weight) / cut_weight
            if cut_weight
            else self.cut
        )
        merged: dict[int, Centroid] = {}
        for local, group in zip(locals_, groups, strict=True):
            merged[group] = (
                merged[group].merged(centroids[local]) if group in merged else centroids[local]
            )
        ids = list(merged)
        global_ids = list(self.globals)
        bonus = 2 * min(len(ids), len(global_ids)) + 1
        weights = [
            [
                bonus + 1 - distance(merged[k], self.globals[g])
                if distance(merged[k], self.globals[g]) <= cut
                else 0.0
                for g in global_ids
            ]
            for k in ids
        ]
        matches = {
            ids[r]: global_ids[c] for r, c in maximize_assignment(weights) if weights[r][c] > 0
        }
        if len(self.globals) + len(ids) - len(matches) > MAX_SPEAKERS:
            raise LimitError("limit")
        updated = dict(self.globals)
        for group in ids:
            label = matches.setdefault(group, len(updated) + 1)
            updated[label] = (
                updated[label].merged(merged[group]) if label in updated else merged[group]
            )
        local_map = {local: matches[group] for local, group in zip(locals_, groups, strict=True)}
        output = [[a + offset_ms, b + offset_ms, f"S{local_map[k]}"] for a, b, k in ordered]
        self.globals = updated
        self.cut, self.cut_weight = cut, cut_weight
        for label in set(local_map.values()):
            self.observations.setdefault(label, set()).add(self.blocks)
        self.blocks += 1
        self.segments += len(turns)
        self.end_ms = max((b + offset_ms for _, b, _ in turns), default=offset_ms)
        return output

    def finish(self) -> tuple[dict[str, str], int]:
        """Ordinary consolidation, then distance-gated uncertain singletons.

        Anchors are fixed before the second stage: small outliers cannot drag
        a reliable mean toward themselves or chain through other outliers.
        No anchor means no relaxed merging. Canonical identities always use
        the smallest original number; finish does not mutate linking state.
        """
        keys = list(self.globals)
        groups = merge_at_cut(list(self.globals.values()), self.cut)
        centers: dict[int, Centroid] = {}
        seen: dict[int, set[int]] = {}
        for key, group in zip(keys, groups, strict=True):
            canonical = keys[group]
            center = self.globals[key]
            centers[canonical] = (
                centers[canonical].merged(center) if canonical in centers else center
            )
            seen.setdefault(canonical, set()).update(self.observations.get(key, set()))
        support = self.profile.min_support_s
        anchors = [key for key, center in centers.items() if center.duration >= support]
        parents = {key: key for key in centers}

        def root(key: int) -> int:
            while parents[key] != key:
                key = parents[key]
            return key

        for key, center in centers.items():
            if key in anchors or len(seen[key]) >= 2 or not anchors:
                continue
            value, nearest = min((distance(center, centers[a]), a) for a in anchors)
            # A fully supported centroid must obey the ordinary ceiling;
            # shorter single-block evidence approaches the geometric boundary
            # between positive similarity and orthogonality, never beyond it.
            bound = 1 - (1 - self.profile.merge_ceiling) * center.duration / support
            if value <= bound:
                first, second = sorted((root(key), root(nearest)))
                parents[second] = first
        label_map = {
            f"S{key}": f"S{root(keys[group])}" for key, group in zip(keys, groups, strict=True)
        }
        return label_map, len(set(label_map.values()))
