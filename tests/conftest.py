import sys
from pathlib import Path

# Make src/dihi importable for all tests
sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "dihi"))
