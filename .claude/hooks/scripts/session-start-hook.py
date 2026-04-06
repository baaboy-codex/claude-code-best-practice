#!/usr/bin/env python3
"""
Session Start Hook
Called when a new session starts
"""

import sys
import json
from datetime import datetime
import os


def log_session_start():
    """Log session start information."""
    log_file = ".claude/hooks/logs/sessions.log"
    os.makedirs(".claude/hooks/logs", exist_ok=True)

    timestamp = datetime.now().isoformat()
    cwd = os.getcwd()

    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] Session started at {cwd}\n")


if __name__ == "__main__":
    log_session_start()
