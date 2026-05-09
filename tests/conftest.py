"""把 project root 加進 sys.path，讓 tests 可以 import 模組"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
