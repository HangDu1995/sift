"""Tests for shared i18n keyword tables (P2-13).

词表是 compliance/extractor.py 与 static_analysis/checks.py 的共享契约 ——
锁住内容,防止有人误改一处忘另处。
"""

from __future__ import annotations

from sift.i18n import ANTI_FABRICATION_CN


class TestI18nKeywords:
    def test_anti_fabrication_cn_contents(self):
        # 反捏造词 —— extractor(提取指令)+ checks(静态检查)共用
        for w in ("捏造", "编造", "虚构", "伪造", "臆造"):
            assert w in ANTI_FABRICATION_CN

    def test_anti_fabrication_cn_well_formed(self):
        assert isinstance(ANTI_FABRICATION_CN, tuple)
        assert len(ANTI_FABRICATION_CN) >= 5
        assert all(isinstance(w, str) and w for w in ANTI_FABRICATION_CN)
