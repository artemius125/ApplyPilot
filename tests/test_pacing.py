"""Deterministic tests for :mod:`applypilot.pacing` (no real sleeping)."""

from __future__ import annotations

import random

from applypilot.pacing import daily_cap_reached, next_delay


def test_base_delay_within_bounds() -> None:
    rng = random.Random(1234)
    for i in range(200):
        delay = next_delay(rng, min_seconds=2.0, max_seconds=5.0, index=i)
        assert 2.0 <= delay <= 5.0


def test_reversed_bounds_are_swapped() -> None:
    rng = random.Random(7)
    delay = next_delay(rng, min_seconds=5.0, max_seconds=2.0, index=0)
    assert 2.0 <= delay <= 5.0


def test_negative_bounds_clamped_to_zero() -> None:
    rng = random.Random(9)
    for i in range(50):
        delay = next_delay(rng, min_seconds=-3.0, max_seconds=-1.0, index=i)
        assert delay == 0.0


def test_long_pause_disabled_by_default() -> None:
    # With long_pause_every=0 the result must stay a plain uniform draw,
    # even at indices that would otherwise trigger a pause.
    for index in (0, 5, 10, 20):
        rng = random.Random(42)
        expected = random.Random(42).uniform(1.0, 2.0)
        delay = next_delay(rng, min_seconds=1.0, max_seconds=2.0, index=index)
        assert delay == expected
        assert 1.0 <= delay <= 2.0


def test_long_pause_added_only_on_multiples() -> None:
    every = 10
    base_max = 2.0
    long_min = 30.0
    for index in range(41):
        rng = random.Random(100)
        delay = next_delay(
            rng,
            min_seconds=1.0,
            max_seconds=base_max,
            index=index,
            long_pause_every=every,
            long_pause_min=long_min,
            long_pause_max=60.0,
        )
        if index > 0 and index % every == 0:
            # index 10, 20, 30, 40 -> long pause added on top of the base.
            assert delay >= long_min + 1.0
        else:
            # index 5 (and others) -> no long pause.
            assert 1.0 <= delay <= base_max


def test_long_pause_not_added_at_index_zero() -> None:
    rng = random.Random(5)
    delay = next_delay(
        rng,
        min_seconds=1.0,
        max_seconds=2.0,
        index=0,
        long_pause_every=10,
        long_pause_min=30.0,
        long_pause_max=60.0,
    )
    assert 1.0 <= delay <= 2.0


def test_long_pause_value_matches_manual_draw() -> None:
    every = 10
    rng = random.Random(555)
    delay = next_delay(
        rng,
        min_seconds=1.0,
        max_seconds=2.0,
        index=every,
        long_pause_every=every,
        long_pause_min=30.0,
        long_pause_max=60.0,
    )
    ref = random.Random(555)
    expected = ref.uniform(1.0, 2.0) + ref.uniform(30.0, 60.0)
    assert delay == expected


def test_daily_cap_reached_basic() -> None:
    assert daily_cap_reached(10, 10) is True
    assert daily_cap_reached(11, 10) is True
    assert daily_cap_reached(9, 10) is False


def test_daily_cap_disabled_when_per_day_non_positive() -> None:
    assert daily_cap_reached(0, 0) is False
    assert daily_cap_reached(1000, 0) is False
    assert daily_cap_reached(1000, -5) is False


def test_daily_cap_boundary_zero_count() -> None:
    assert daily_cap_reached(0, 1) is False
    assert daily_cap_reached(1, 1) is True
