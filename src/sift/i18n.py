"""共享的国际化词表 —— 跨模块复用的关键词单一来源。

只有被多个模块共用的词才放这里,避免重复定义导致不一致(改一处忘另处,且各自
测试绿查不出来)。单模块独用的词表留在各自模块(如 static_analysis/checks.py 的
trigger_words、compliance/extractor.py 的 constraint/tool 词)。

加第三门语言时,可升级为按语言分组 / locales 资源文件;当前仅 en+zh 且词表小,
保持最简(YAGNI)。
"""

from __future__ import annotations

# 反捏造相关中文词。
# compliance/extractor.py(提取反捏造指令)与 static_analysis/checks.py(静态检查
# 是否含反捏造指导)共用 —— 改这里两处同步生效,避免重复定义。
ANTI_FABRICATION_CN: tuple[str, ...] = ("捏造", "编造", "虚构", "伪造", "臆造")
