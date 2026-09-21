"""统计算子:Wilson 置信区间 + NormalizedGain。"""

import math

from sift.stats import normalized_gain, wilson_ci


class TestWilsonCI:
    @staticmethod
    def test_no_samples_returns_none():
        assert wilson_ci(0, 0) == (None, None)

    @staticmethod
    def test_all_success_upper_bound_is_one():
        lo, hi = wilson_ci(10, 10)
        assert math.isclose(hi, 1.0, abs_tol=1e-9)
        assert 0.6 < lo < 1.0

    @staticmethod
    def test_all_fail_lower_bound_is_zero():
        lo, hi = wilson_ci(0, 10)
        assert math.isclose(lo, 0.0, abs_tol=1e-9)
        assert 0 < hi < 0.4

    @staticmethod
    def test_known_value_k7_n10():
        # 手算:k=7, n=10, 95% → [0.397, 0.892]
        lo, hi = wilson_ci(7, 10)
        assert math.isclose(lo, 0.397, abs_tol=0.01)
        assert math.isclose(hi, 0.892, abs_tol=0.01)

    @staticmethod
    def test_significant_when_ci_above_half():
        # k=9, n=10 → 下界 > 0.5(A 显著赢)
        lo, hi = wilson_ci(9, 10)
        assert lo > 0.5

    @staticmethod
    def test_not_significant_straddles_half():
        # k=5, n=10 → CI 含 0.5(不显著,样本不够)
        lo, hi = wilson_ci(5, 10)
        assert lo <= 0.5 <= hi


class TestNormalizedGain:
    @staticmethod
    def test_high_ceiling_amplifies_small_raw_delta():
        # 0.9→0.95:raw delta 0.05,gain 0.5(放大)
        assert math.isclose(normalized_gain(0.9, 0.95), 0.5, abs_tol=0.001)

    @staticmethod
    def test_low_floor_shrinks_same_raw_delta():
        # 0.1→0.15:raw delta 也 0.05,gain ~0.056(缩小)
        assert math.isclose(normalized_gain(0.1, 0.15), 0.0556, abs_tol=0.001)

    @staticmethod
    def test_midpoint():
        assert math.isclose(normalized_gain(0.5, 0.6), 0.2, abs_tol=0.001)

    @staticmethod
    def test_pre_at_ceiling_no_gain():
        assert normalized_gain(1.0, 1.0) == 0.0

    @staticmethod
    def test_post_full_score():
        assert normalized_gain(0.5, 1.0) == 1.0

    @staticmethod
    def test_no_change():
        assert normalized_gain(0.8, 0.8) == 0.0

    @staticmethod
    def test_negative_delta_regression():
        # 退步 0.6→0.5:(0.5-0.6)/(1-0.6) = -0.25
        assert math.isclose(normalized_gain(0.6, 0.5), -0.25, abs_tol=0.001)
