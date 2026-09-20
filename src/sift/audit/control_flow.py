"""control_flow: 控制流判定的纯程序检测层(ORDER 顺序对 + QUANTITY 数量指令)。

两个检测器只吃 refine 后的**静态** instructions(derived 恒 REFERENCE_USAGE、反推范围仅
required_reads,天然不沾两检测域),产出供控制流判官(auditor._judge_control_flow)消费:

- ``detect_order_pairs``:同 section 相邻 HEADING_STEP 里含**显式**顺序连词的两条 → pair。
  词面只做召回候选;判定全在判官(宁少报:无显式连词不判——文档顺序 ≠ 强制顺序,
  数字编号列表(1. 2. 3.)是排版惯例,不算顺序声明)。
- ``detect_quantity``:检测文本(source_excerpt 优先,数量词多在被切走的 bullet 前缀里)
  命中数量模式 → count 型(阈值可程序比对)/ distributive 型(逐项分布)。

纯函数、不调 LLM、不读 transcript——确定、可单测,对齐 dedup.py / noise.py 定位。
"""

from __future__ import annotations

import re
from typing import NamedTuple

from sift.models.batch_models import InstructionCheck, InstructionSource

# 显式顺序连词(词表冻结;补词改这里,别在调用点散写)。两个实现细节:
# - 中文排除用负向 lookaround 而非 \b:Python \b 基于 \w,CJK 属 \w,中文词内部无边界
#   (先例:extractor 的「不可(?!用|见|…)」)。「优先(级)/预先」含「先」、「不再」含
#   「再」——它们不是顺序声明,命中只会造假 pair 白耗判官调用。
# - 英文 token 必须加 \b(大小写不敏感):「then」不得命中「authentic」。
_ORDER_RE = re.compile(
    r"(?<![优预])先"  # 排除 优先/预先(lookbehind);「首先」命中是良性的(真是顺序词)
    r"|然后"
    r"|(?<!不)再"  # 排除 不再(否定义);「再检查一遍」类 again 副词由判官 na/pass 兜住
    r"|接着|随后|之后|其次|下一步|最后"
    r"|\b(?:first|then|next|after|finally|until)\b",
    re.IGNORECASE,
)

# count 型:阈值可程序比对。N 只认 ASCII 数字——中文数字(「至少两次」)首期不支持
# (记档在 spec「明确不做」)。全部要求「N 次/N 遍」单位(裸数字「不少于 3」数的什么
# 不可比,不勉强);操作词与数字之间允许 ≤8 个非标点字符(自然语序「至少**调用** 2 次」)。
# 检查顺序 gte → lte → eq(先命中先赢,「至少 3 次」不落 eq)。
_GTE_RE = re.compile(r"(?:至少|不少于)[^,。;、\n]{0,8}?(\d+)\s*[次遍]")
_LTE_RE = re.compile(r"(?:至多|不超过)[^,。;、\n]{0,8}?(\d+)\s*[次遍]")
_EQ_RE = re.compile(r"(\d+)\s*[次遍]")

# distributive 型:逐项分布(「每个/所有」)。「所有」在约束文本里常见且语义就是全称
# 量化,命中即移交;误命中面由判官 na(无可数对象)兜底。
_DISTRIBUTIVE_RE = re.compile(r"每个|所有|逐个|各一|\bevery\b|\ball\b|\beach\b", re.IGNORECASE)

_OP_LABEL = {"gte": "至少", "lte": "至多", "eq": "恰好"}


class OrderPair(NamedTuple):
    """一对有显式顺序声明相邻步骤(A 须先于 B)。"""

    a: InstructionCheck
    b: InstructionCheck


class QuantitySpec(NamedTuple):
    """一条带数量期望的指令的检测元数据(kind: count=阈值可比对 / distributive=逐项分布)。"""

    kind: str
    op: str = ""  # count 型: "gte" | "lte" | "eq"
    threshold: int = 0  # count 型阈值


def context_text(inst: InstructionCheck) -> str:
    """检测/判官条目用文本:source_excerpt 优先(含被切走的 bullet 前缀主语,数量词
    多在那里),但仅当 excerpt 确实**包含**本条指令(同 bullet)——body 级指令的
    excerpt 是 nearest preceding bullet,不含本条指令文,用它会拿**邻条**的文本当
    本条要求(误检/误判);此时回退 instruction_text(本条真实要求)。
    """
    excerpt = inst.source_excerpt or ""
    return excerpt if inst.instruction_text in excerpt else inst.instruction_text


# 检测层内部别名(历史名,检测函数内用)
_detect_text = context_text


def order_instruction_text(a: InstructionCheck, b: InstructionCheck) -> str:
    """ORDER finding 的合成 instruction_text(A/B 用 headline,空回退 instruction_text)。"""
    a_disp = a.headline or a.instruction_text
    b_disp = b.headline or b.instruction_text
    return f"(顺序)「{a_disp}」须先于「{b_disp}」"


def detect_order_pairs(instructions: list[InstructionCheck]) -> list[OrderPair]:
    """同 section 相邻 HEADING_STEP 里含显式顺序连词的两条 → pair 列表。

    相邻口径 = **refine 后 HEADING_STEP 子序列中相邻**(InstructionCheck 不携带原文下标、
    refine 只删不重排,「原文相邻」不可恢复;extractor 按文档序逐 section 前置产出,该口径
    是可实现物)。分组按**连续同 section 段**:同名字标题出现两次时不跨段配对(按 section
    字符串全局分组会跨段)。同 bullet 内的「先A再B」已是单条指令,归 step 判官整体判,不拆。

    命中条件 = 两条 bullet 文本(source_excerpt 优先)任一含顺序连词;**无显式连词不判**
    (文档顺序 ≠ 强制顺序,宁少报)。纯词面,不做语义消歧——假 pair 由判官 na 兜底。
    """
    pairs: list[OrderPair] = []
    run: list[InstructionCheck] = []

    def _flush() -> None:
        for x, y in zip(run, run[1:]):
            if not (_ORDER_RE.search(_detect_text(x)) or _ORDER_RE.search(_detect_text(y))):
                continue
            pairs.append(OrderPair(x, y))

    for inst in instructions:
        if inst.source != InstructionSource.HEADING_STEP:
            continue  # 子序列口径:非 HEADING_STEP 指令不截断相邻性
        if run and run[-1].section != inst.section:
            _flush()
            run = []
        run.append(inst)
    _flush()
    return pairs


def detect_quantity(instructions: list[InstructionCheck]) -> list[tuple[InstructionCheck, QuantitySpec]]:
    """检测带数量期望的指令 → (指令, 元数据) 列表;命中者移交控制流判官(不进原判官桶)。

    检测文本 = source_excerpt or instruction_text。OUTPUT_FORMAT 不移交(它走确定性
    子串匹配,数量指令进 output 桶无判定面);derived 不入此函数(调用方只喂静态
    instructions)。只检测、不改 instruction_text(params 的 quantity 元数据由判官侧
    使用,refine/headline 缓存键均不含 params,不受影响)。
    """
    out: list[tuple[InstructionCheck, QuantitySpec]] = []
    for inst in instructions:
        if inst.source == InstructionSource.OUTPUT_FORMAT:
            continue
        text = _detect_text(inst)
        if not text:
            continue
        if m := _GTE_RE.search(text):
            out.append((inst, QuantitySpec("count", "gte", int(m.group(1)))))
        elif m := _LTE_RE.search(text):
            out.append((inst, QuantitySpec("count", "lte", int(m.group(1)))))
        elif m := _EQ_RE.search(text):
            out.append((inst, QuantitySpec("count", "eq", int(m.group(1)))))
        elif _DISTRIBUTIVE_RE.search(text):
            out.append((inst, QuantitySpec("distributive")))
    return out


def quantity_op_label(op: str) -> str:
    """count 型 op 的人话标签(程序判 evidence 用,如 gte → 「至少」)。"""
    return _OP_LABEL.get(op, op)
