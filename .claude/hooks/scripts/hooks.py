#!/usr/bin/env python3
"""
Claude Code Hooks Handler
Handles hook events for Claude Code lifecycle

Usage:
    python hooks.py <event-name> [json-payload]
"""

import json
import sys
import os
from datetime import datetime

HOOKS_CONFIG = os.path.join(os.path.dirname(__file__), "../config/hooks-config.json")
LOCAL_CONFIG = os.path.join(os.path.dirname(__file__), "../config/hooks-config.local.json")


def load_config():
    """Load hook configuration from JSON files."""
    config = {}

    if os.path.exists(HOOKS_CONFIG):
        with open(HOOKS_CONFIG, 'r') as f:
            config.update(json.load(f))

    if os.path.exists(LOCAL_CONFIG):
        with open(LOCAL_CONFIG, 'r') as f:
            local = json.load(f)
            for key, value in local.get("hooks", {}).items():
                if key in config.get("hooks", {}):
                    config["hooks"][key].update(value)

    return config


def log_event(event, payload):
    """Log hook event to file."""
    log_dir = os.path.join(os.path.dirname(__file__), "../logs")
    os.makedirs(log_dir, exist_ok=True)

    log_file = os.path.join(log_dir, "hooks.log")
    timestamp = datetime.now().isoformat()

    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {event}: {json.dumps(payload or {})}\n")


def play_sound(sound_name):
    """Play a sound for the hook event."""
    sound_file = os.path.join(os.path.dirname(__file__), f"../sounds/{sound_name}")
    if os.path.exists(sound_file):
        try:
            import subprocess
            if sys.platform == "win32":
                import winsound
                winsound.PlaySound(sound_file, winsound.SND_FILENAME)
            else:
                subprocess.run(["afplay", sound_file], capture_output=True)
        except Exception:
            pass


def handle_hook(event, payload):
    """Main hook handler."""
    config = load_config()

    hooks = config.get("hooks", {})
    hook_config = hooks.get(event, {})

    if not hook_config.get("enabled", False):
        return

    log_event(event, payload)

    if hook_config.get("sound"):
        play_sound(hook_config["sound"])

    if event == "SessionStart" and hook_config.get("logContext"):
        log_context_info()


def log_context_info():
    """Log current context information on session start."""
    log_dir = os.path.join(os.path.dirname(__file__), "../logs")
    os.makedirs(log_dir, exist_ok=True)

    context_file = os.path.join(log_dir, "context.log")
    timestamp = datetime.now().isoformat()

    with open(context_file, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] Session started\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: hooks.py <event-name> [json-payload]")
        sys.exit(1)

    event_name = sys.argv[1]
    payload = json.loads(sys.argv[2]) if len(sys.argv) > 2 else None

    handle_hook(event_name, payload)
