from sift.cli.main import app

# import 顺序 = --help 分区(panel)的显示顺序 = 工作流顺序:
# 静态体检 → 出题 → 评测 → 真任务 → 审计 → 判决 → 触发 → 看结果
import sift.cli.commands.analyze  # noqa: F401
import sift.cli.commands.vet  # noqa: F401
import sift.cli.commands.critique  # noqa: F401
import sift.cli.commands.init  # noqa: F401
import sift.cli.commands.add  # noqa: F401
import sift.cli.commands.inspect  # noqa: F401
import sift.cli.commands.freeze  # noqa: F401
import sift.cli.commands.coverage  # noqa: F401
import sift.cli.commands.run  # noqa: F401  # eval + run(评测 → 真任务)
import sift.cli.commands.audit  # noqa: F401
import sift.cli.commands.gate  # noqa: F401
import sift.cli.commands.trigger  # noqa: F401
import sift.cli.commands.report  # noqa: F401
