"""Path setup for every test module in this package.

The service modules import each other as ``services.*`` and ``models.*``, so
``backend/`` must be on ``sys.path`` before any test module is imported. pytest
imports this file before collecting anything, so putting it here means a new
test file needs no boilerplate and cannot reintroduce the ordering bug this
package has hit twice: a module that did not insert the path itself only worked
when some alphabetically earlier module happened to have done it already, and a
new test whose name sorted first failed with a bare
``ModuleNotFoundError: No module named 'services'``.

This does not cover ``unittest discover -s backend/tests``, which makes this
directory the top-level and so runs no package init or conftest. Modules must
keep their own sys.path guard for that runner; the guard is idempotent, so
having both is harmless.
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
