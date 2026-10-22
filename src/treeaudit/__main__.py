"""Allow ``python -m treeaudit``."""

import sys

from treeaudit.entry import run

sys.exit(run())
