from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn

_ROOT = Path(__file__).resolve().parents[2]
# Settings load `.env` relative to the current working directory. Playwright
# launches this server from `frontend/`, so normalize to the project root
# before importing modules that initialize the shared database pool.
os.chdir(_ROOT)
for candidate in (str(Path(__file__).resolve().parent), str(_ROOT), str(_ROOT / "src")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from support import external_e2e_harness


def main() -> None:
    host = os.environ.get("TASK1_E2E_HOST", "127.0.0.1")
    port = int(os.environ.get("TASK1_E2E_PORT", "8000"))
    with external_e2e_harness("task1_phase6b_ui") as harness:
        uvicorn.run(
            harness.app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )


if __name__ == "__main__":
    main()
