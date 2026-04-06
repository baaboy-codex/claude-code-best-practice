#!/usr/bin/env python3
"""
Stop Hook
Called when Claude stops responding
"""

import sys
import json
from datetime import datetime


def log_stop(reason, payload):
    """Log when Claude stops."""
    log_file = ".claude/hooks/logs/stop.log"
    import os
    os.makedirs(".claude/hooks/logs", exist_ok=True)

    timestamp = datetime.now().isoformat()
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] Stop: {reason}\n")
        if payload:
            f.write(f"  Payload: {json.dumps(payload)[:500]}\n")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        payload = json.loads(sys.argv[1]) if sys.argv[1] != "null" else {}
    else:
        payload = {}

    log_stop(payload.get("reason", "unknown"), payload)
