"""RAVAN cloud backend package."""
import sys
from pathlib import Path

# Render starts uvicorn from the repository root, but tests and `python -m`
# may not. Making the root importable here is what lets the server reuse
# core/gemini.py, core/prompt.txt and core/viseme.py without copying them.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
