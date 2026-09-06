"""Launch the Streamlit UI (dev convenience; Dockerfile.ui runs streamlit directly)."""

import os
import subprocess
import sys

subprocess.run(
    [
        sys.executable, "-m", "streamlit", "run", "ui/app.py",
        "--server.port", os.getenv("UI_PORT", "8501"),
        "--server.address", os.getenv("UI_ADDRESS", "0.0.0.0"),
        "--server.headless", "true",
        "--browser.gatherUsageStats=false",
    ],
    check=True,
)