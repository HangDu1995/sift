"""ComparisonSummary.from_comparisons:win-rate 聚合 + Wilson CI + failed 排除分母。"""

from sift.models import ComparisonResult
from sift.models.batch_models import ComparisonSummary


def _comps(*winners, failed_set=()):
    """构造比较列表:winners=每次 compare 的 winner(with/without/tie);failed_set=哪些是失败比较。"""
    return [ComparisonResult(winner=w, reasoning="r", failed=(i in failed_set)) for i, w in enumerate(winners)]


class TestFromComparisonsWilsonCI:
    @staticmethod
    def test_significant_a_win():
        # 9 赢 1 平 → win_rate 0.9,下界 > 0.5(A 显著更优)
        s = ComparisonSummary.from_comparisons(_comps(*(["with"] * 9), "tie"))
        assert s.valid == 10 and s.a_wins == 9
        assert s.win_rate == 0.9
        assert s.win_rate_ci_lo > 0.5

    @staticmethod
    def test_not_significant_straddles_half():
        # 5 赢 5 平 → win_rate 0.5,CI 含 0.5(不显著,样本不够判 A/B)
        s = ComparisonSummary.from_comparisons(_comps(*(["with"] * 5), *(["tie"] * 5)))
        assert s.win_rate == 0.5
        assert s.win_rate_ci_lo <= 0.5 <= s.win_rate_ci_hi

    @staticmethod
    def test_b_significant():
        # 9 输 1 平 → B 显著更优(win_rate=0,上界 < 0.5)
        s = ComparisonSummary.from_comparisons(_comps(*(["without"] * 9), "tie"))
        assert s.b_wins == 9
        assert s.win_rate_ci_hi < 0.5

    @staticmethod
    def test_all_failed_no_valid_no_ci():
        # 全 failed → valid=0,win_rate None,CI None(失败≠打平,排除分母)
        s = ComparisonSummary.from_comparisons(_comps("with", "without", failed_set={0, 1}))
        assert s.valid == 0
        assert s.win_rate is None
        assert s.win_rate_ci_lo is None and s.win_rate_ci_hi is None

    @staticmethod
    def test_failed_excluded_from_denominator():
        # 9 赢 + 1 failed → failed 排除,valid=9,win_rate=1.0
        s = ComparisonSummary.from_comparisons(_comps(*(["with"] * 9), "with", failed_set={9}))
        assert s.failed == 1 and s.valid == 9
        assert s.win_rate == 1.0
        assert s.win_rate_ci_lo > 0.5
