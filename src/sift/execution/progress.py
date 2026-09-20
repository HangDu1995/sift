"""执行层发布的具名进度事件，终端渲染器和其他消费者共用。"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProgressUpdate:
    """一次进度通知中的执行信息和完成计数。"""

    eval_name: str
    config_name: str
    eval_idx: int
    total_evals: int
    completed: int
    total: int
    grading: Any
    run_duration: float = 0.0
    run_output: Any = None
    phase: str = ""
    substep: str = ""
