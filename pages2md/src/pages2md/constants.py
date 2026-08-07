import os
import subprocess
from pathlib import Path


MODEL_ID = "baidu/Unlimited-OCR"
MODEL_REVISION = "07dea832e22aefee32ad281d4b80551282e1c168"
MLX_VLM_REVISION = "fbdfc837da0ee197a18859ea327ede858631bdb1"
AUTO_SPLIT_BYTES = 256 * 1024
DEFAULT_DPI = 300
SCHEMA_VERSION = 2


def commit_version() -> str:
    """Return the source commit injected by Nix or discovered from a checkout."""
    injected = os.environ.get("PAGES2MD_VERSION")
    if injected:
        return injected
    try:
        return subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        return "unknown"
