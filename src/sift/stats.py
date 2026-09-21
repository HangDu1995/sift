"""统计算子:Wilson 置信区间 + NormalizedGain。

Wilson score 区间比正态近似(p±z·sqrt(p(1-p)/n))更适合 sift 的小样本(N=2~5)
二值场景(过/挂、赢/输)——后者在 p≈0/1 或 n 小时跑飞;Wilson 小样本稳。
NormalizedGain(Hake 1998)修 raw delta 的天花板效应:0.9→0.95 比 0.1→0.15 难,
但 raw delta 都显 0.05,高位改进被系统性低估。
"""

from __future__ import annotations

import math

# 双侧 z 值(置信水平 → z)
_Z = {0.95: 1.959964, 0.99: 2.575829}


def wilson_ci(k: int, n: int, conf: float = 0.95) -> tuple[float | None, float | None]:
    """Wilson score 区间:k 次成功 / n 次试验的二值比例的置信区间(默认 95%)。

    返回 (lo, hi)。n<=0(无样本)→ (None, None)。
    胜率显著性判定(调用方):lo > 0.5 → A 显著更优;hi < 0.5 → B 显著更优;
    lo <= 0.5 <= hi → 不显著(样本不足以判 A/B 谁赢)。
    """
    if n <= 0:
        return (None, None)
    z = _Z.get(conf, _Z[0.95])
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    # 比例区间理论 ∈ [0,1],浮点误差可能微超(p≈0/1 时),clamp 干净化。
    return (max(0.0, center - margin), min(1.0, center + margin))


def normalized_gain(pre: float, post: float) -> float:
    """NormalizedGain = (post - pre) / (1 - pre)。

    pre=基线(baseline)通过率,post=改进后(with skill)通过率。
    pre>=1(基线已封顶,无可改进)→ 0;post>=1(满绩)→ 1;pre==post → 0。
    负 delta(退步)同样成立(返回负值)。
    """
    if pre >= 1.0:
        return 0.0
    if post >= 1.0:
        return 1.0
    return (post - pre) / (1 - pre)
