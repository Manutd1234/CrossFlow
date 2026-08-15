"""Smooth, deterministic variation for live telemetry.

Replaces `random.uniform(-3, 3)`, which re-rolled on every request. At an 8
second poll that made the congestion index flicker several points in each
direction — which reads as a random number generator, not a sensor.

This is value noise: hash-derived anchors at fixed time buckets, smoothly
interpolated. Two properties matter for the demo:

  * Deterministic in (key, time). Two polls a second apart agree to the
    displayed decimal, two browser tabs agree with each other, and the value
    survives a `uvicorn --reload`.
  * Continuous. Over minutes it wanders the full amplitude, so the dashboard
    visibly breathes without ever jumping.
"""

import zlib
from datetime import datetime

# zlib.crc32, deliberately, rather than the built-in hash(): Python salts
# string hashing per process, so hash() would produce different telemetry in
# every worker and make the tests flaky.
def _anchor(key: str, bucket: int) -> float:
    """Stable pseudo-random value in [-1, 1] for a (key, bucket) pair."""
    digest = zlib.crc32(f"{key}:{bucket}".encode())
    return (digest / 0xFFFFFFFF) * 2.0 - 1.0


def _smoothstep(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


def smooth_noise(key: str, t_epoch: float, period_s: float = 300.0) -> float:
    """Continuous noise in [-1, 1], varying over `period_s`."""
    pos = t_epoch / period_s
    bucket = int(pos // 1)
    frac = pos - bucket
    a = _anchor(key, bucket)
    b = _anchor(key, bucket + 1)
    return a + (b - a) * _smoothstep(frac)


def corridor_drift(corridor_id: str, now: datetime, amplitude: float = 4.0) -> float:
    """Per-corridor congestion offset in points.

    Two octaves: a slow five-minute swell plus a light 71-second ripple, so the
    number moves enough to look live without the ripple dominating.
    """
    t = now.timestamp()
    slow = smooth_noise(corridor_id, t, period_s=300.0)
    fast = smooth_noise(f"{corridor_id}:fast", t, period_s=71.0)
    return amplitude * (slow * 0.78 + fast * 0.22)
