"""Three-parameter logistic IRT: probability, information, and EAP ability estimation.

Every test starts every student at the same neutral prior (θ ~ N(0, 1)); nothing
from earlier tests is used. The estimate is the posterior mean over a fixed grid,
which stays finite even when every answer so far is right, or every one wrong.
"""

import math
from dataclasses import dataclass

GRID_MIN, GRID_MAX, GRID_STEP = -4.0, 4.0, 0.1
GRID: tuple[float, ...] = tuple(round(GRID_MIN + i * GRID_STEP, 10) for i in range(int((GRID_MAX - GRID_MIN) / GRID_STEP) + 1))
PRIOR: tuple[float, ...] = tuple(math.exp(-t * t / 2) for t in GRID)


@dataclass(frozen=True)
class ItemParams:
    a: float  # discrimination
    b: float  # difficulty
    c: float  # guessing floor


@dataclass(frozen=True)
class Estimate:
    theta: float
    se: float


NEUTRAL = Estimate(theta=0.0, se=1.0)


def p_correct(theta: float, item: ItemParams) -> float:
    z = -item.a * (theta - item.b)
    # math.exp overflows past ~709; the probability is already 0 or 1 long before that.
    logistic = 0.0 if z > 700 else 1.0 / (1.0 + math.exp(z))
    return item.c + (1.0 - item.c) * logistic


def information(theta: float, item: ItemParams) -> float:
    """Fisher information the item gives at θ: highest near its difficulty, zero far away."""
    p = p_correct(theta, item)
    q = 1.0 - p
    if p <= 0.0 or q <= 0.0 or item.c >= 1.0:
        return 0.0
    return item.a**2 * ((p - item.c) / (1.0 - item.c)) ** 2 * q / p


def estimate(responses: list[tuple[ItemParams, bool]]) -> Estimate:
    """EAP estimate and its standard error from (item, answered-correctly) pairs."""
    if not responses:
        return NEUTRAL
    posterior = list(PRIOR)
    for item, correct in responses:
        for i, theta in enumerate(GRID):
            p = p_correct(theta, item)
            posterior[i] *= p if correct else 1.0 - p
        # Renormalise every step so long tests never underflow to zero.
        total = sum(posterior)
        if total <= 0.0:
            return NEUTRAL
        posterior = [w / total for w in posterior]

    mean = sum(t * w for t, w in zip(GRID, posterior))
    variance = sum((t - mean) ** 2 * w for t, w in zip(GRID, posterior))
    return Estimate(theta=mean, se=math.sqrt(max(variance, 0.0)))


def scaled_score(theta: float) -> int:
    """The number people see: 500 + 100θ, kept within 200-800."""
    return int(round(min(max(500 + 100 * theta, 200), 800)))


def level_for(theta: float) -> str:
    """Default performance band until an institution sets its own cut scores."""
    if theta < -1.0:
        return "beginning"
    if theta < 0.0:
        return "developing"
    if theta < 1.0:
        return "proficient"
    return "advanced"
