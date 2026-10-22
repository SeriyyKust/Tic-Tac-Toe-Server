"""Elo with K=32, integer scores and symmetric rounding."""
import math

K = 32


def elo_delta(x_rating, o_rating, score):
    difference = max(-8000, min(8000, o_rating - x_rating))
    expected = 1 / (1 + 10 ** (difference / 400))
    raw = K * (score - expected)
    delta = int(math.copysign(math.floor(abs(raw) + 0.5), raw))
    # Transfer the same number of points between players, never below zero.
    return max(-x_rating, min(o_rating, delta))
