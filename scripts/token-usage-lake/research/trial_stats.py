# Promoted from the Token Diet Notes prototype; canonical executable source is here.
"""Small reproducible statistical checks. No call-level independence fiction."""

import math
import random
from statistics import NormalDist


def holm(pvalues):
    ordered = sorted(pvalues.items(), key=lambda kv: kv[1])
    answer = {}
    last = 0
    for i, (k, p) in enumerate(ordered):
        if isinstance(p, bool) or not math.isfinite(p) or not 0 <= p <= 1:
            raise ValueError("invalid p-value")
        last = max(last, min(1, (len(ordered) - i) * p))
        answer[k] = last
    return answer


WILSON_Z = NormalDist().inv_cdf(0.975)


def wilson(successes, total, z=WILSON_Z):
    if (
        not isinstance(successes, int)
        or isinstance(successes, bool)
        or not isinstance(total, int)
        or isinstance(total, bool)
        or total <= 0
        or not 0 <= successes <= total
    ):
        raise ValueError("invalid binary counts")
    p = successes / total
    d = 1 + z * z / total
    center = (p + z * z / (2 * total)) / d
    radius = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / d
    return max(0, center - radius), min(1, center + radius)


def quality_gate(treatment, control, floor=0.95, margin=0.02, unknown=0):
    if unknown:
        return dict(pass_gate=False, reason="unknown or censored acceptance")
    tl, tu = wilson(*treatment)
    cl, cu = wilson(*control)
    # Union of the two 97.5% one-sided Wilson bounds is conservative at 95%.
    lower = tl - cu
    return dict(
        pass_gate=tl >= floor and lower >= -margin,
        treatment_success_lower=tl,
        success_difference_lower=lower,
        method=(
            "conservative Wilson difference; NOT a substitute for registered "
            "cluster/multiplicity-adjusted inference"
        ),
    )


def quantile(xs, p):
    x = sorted(xs)
    q = p * (len(x) - 1)
    i = int(q)
    return x[i] + (x[min(i + 1, len(x) - 1)] - x[i]) * (q - i)


def paired_cluster_bootstrap(clusters, metric="tokens", draws=2000, seed=0):
    if len(clusters) < 2 or draws < 100:
        raise ValueError("insufficient clusters/draws")

    def effect(rows):
        a = sum(r["treatment_accepted"] for r in rows)
        b = sum(r["control_accepted"] for r in rows)
        if not a or not b:
            return None
        return (
            sum(r["treatment_" + metric] for r in rows) / a
            - sum(r["control_" + metric] for r in rows) / b
        )

    rng = random.Random(seed)  # noqa: S311 - Reproducible resampling, not security material.
    sample = []
    for _ in range(draws):
        value = effect([rng.choice(clusters) for _ in clusters])
        if value is not None:
            sample.append(value)
    return dict(
        point=effect(clusters),
        interval90=[quantile(sample, 0.05), quantile(sample, 0.95)]
        if len(sample) >= 0.95 * draws
        else None,
        valid_draws=len(sample),
        draws=draws,
        clusters=len(clusters),
        seed=seed,
        assignment_unit="paired independent session cluster",
        calibration_uncertainty_included=False,
        claim="exploratory_unless_registered_unseen_assignment_and_adjustments_verified",
    )
