"""Human-paced delay and daily-limit helpers for auto-apply runs.

These are pure, deterministic (given an ``random.Random``) helpers that only
compute *when* the next action may happen and *whether* a daily budget is
exhausted. They intentionally do nothing beyond randomised timing and counting:
no fingerprinting, no anti-bot evasion, no CAPTCHA handling.
"""

from __future__ import annotations

import random


def next_delay(
    rng: random.Random,
    *,
    min_seconds: float,
    max_seconds: float,
    index: int,
    long_pause_every: int = 0,
    long_pause_min: float = 0.0,
    long_pause_max: float = 0.0,
) -> float:
    """Return the delay (in seconds) to wait before the next action.

    The base delay is ``rng.uniform(min_seconds, max_seconds)``. When
    ``long_pause_every`` is greater than 0 and ``index`` is a positive multiple
    of it (``index > 0 and index % long_pause_every == 0``), an extra long pause
    of ``rng.uniform(long_pause_min, long_pause_max)`` is added on top.

    Validation:
      * if ``min_seconds > max_seconds`` the two bounds are swapped;
      * any negative bound is clamped to ``0``.

    Defaults are backwards compatible: with ``long_pause_every=0`` the result is
    just a plain uniform draw between ``min_seconds`` and ``max_seconds``.

    Args:
        rng: Source of randomness (deterministic when seeded).
        min_seconds: Lower bound of the base delay.
        max_seconds: Upper bound of the base delay.
        index: Zero-based index of the action about to be taken.
        long_pause_every: Add a long pause every N actions; ``0`` disables it.
        long_pause_min: Lower bound of the extra long pause.
        long_pause_max: Upper bound of the extra long pause.

    Returns:
        The number of seconds to wait (always ``>= 0``).
    """
    lo, hi = _sanitize_bounds(min_seconds, max_seconds)
    delay = rng.uniform(lo, hi)

    if long_pause_every > 0 and index > 0 and index % long_pause_every == 0:
        plo, phi = _sanitize_bounds(long_pause_min, long_pause_max)
        delay += rng.uniform(plo, phi)

    return delay


def daily_cap_reached(count: int, per_day: int) -> bool:
    """Return whether the daily action budget has been reached.

    Args:
        count: How many actions were already taken today.
        per_day: The daily cap. A value ``<= 0`` means "no cap".

    Returns:
        ``True`` when ``per_day > 0`` and ``count >= per_day``; otherwise
        ``False`` (including when the cap is disabled with ``per_day <= 0``).
    """
    if per_day <= 0:
        return False
    return count >= per_day


def _sanitize_bounds(low: float, high: float) -> tuple[float, float]:
    """Swap reversed bounds and clamp negatives to zero."""
    if low > high:
        low, high = high, low
    return max(low, 0.0), max(high, 0.0)
