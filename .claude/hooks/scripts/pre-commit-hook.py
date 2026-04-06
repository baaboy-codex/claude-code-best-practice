#!/usr/bin/env python3
"""
Pre-Tool Use Hook
Called before each tool execution
"""

import sys
import json
from datetime import datetime

ALLOWED_TOOLS = {
    "Read", "Write", "Edit", "Bash", "Glob", "Grep",
    "Agent", "Task", "TaskOutput", "TaskStop", "TaskCreate", "TaskList", "TaskGet", "TaskUpdate"
}

DANGEROUS_PATTERNS = [
    "rm -rf",
    "drop table",
    "delete from",
    "format disk",
    "del /f /s /q"
]


def is_safe_command(command):
    """Check if command is safe to execute."""
    cmd_lower = command.lower()
    for pattern in DANGEROUS_PATTERNS:
        if pattern.lower() in cmd_lower:
            return False
    return True


def log_tool_use(tool_name, args):
    """Log tool usage."""
    log_file = ".claude/hooks/logs/tools.log"
    os.makedirs(".claude/hooks/logs", exist_ok=True)

    timestamp = datetime.now().isoformat()
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {tool_name}: {json.dumps(args)[:200]}\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(0)

    payload = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    tool_name = payload.get("tool", "")

    import os

    if tool_name == "Bash":
        command = payload.get("input", {}).get("command", "")
        if command and not is_safe_command(command):
            print(f"Warning: Potentially dangerous command detected: {command[:50]}")
            sys.exit(1)

    log_tool_use(tool_name, payload)
