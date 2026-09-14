"""pytest 全局配置。

显式把项目根目录插进 sys.path，让 `import codesentry` 在未安装包的情况下
也能工作。虽然 pyproject 里已配了 `pythonpath = ["."]`，但那个选项依赖
较新版本的 pytest；这里再兜一层，保证在老环境里也能跑测试。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_configure(config):
    """注册自定义标记，避免 pytest 对未注册标记发出警告。"""
    config.addinivalue_line("markers", "needs_git: 需要本机 git 可用")
