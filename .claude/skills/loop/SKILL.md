---
name: loop
description: Run a prompt or slash command on a recurring interval (e.g. /loop 5m /foo, defaults to 10m)
argument-hint: [interval] [prompt or /command]
user-invocable: true
---
# Loop Skill

Run a prompt or slash command on a recurring interval.

## Usage
```
/loop 5m /some-command
/loop 10m analyze progress
/loop 1h run tests
```

## Interval Format
- `5m` — 5 minutes
- `1h` — 1 hour
- `30m` — 30 minutes
- `1d` — 1 day

## Notes
- Maximum duration is 3 days (72 hours)
- Useful for monitoring, recurring tasks, and long-running processes
- Use `/tasks` to view and manage running loops
