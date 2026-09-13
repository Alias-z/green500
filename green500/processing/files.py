"""Small file-persistence helpers shared by standalone processing commands."""

import json
import os
import time
from pathlib import Path


def save_json(path, value):
    """Publish one complete JSON checkpoint by atomic replacement."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False, default=str)
        + "\n"
    )
    temporary.replace(path)
