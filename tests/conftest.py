"""Test config: point the app at a temp DATA_DIR before anything imports it."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="mewtest_data_")
os.environ.setdefault("DATA_DIR", _TMP)
os.environ["AUTO_BOOTSTRAP"] = "0"
