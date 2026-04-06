---
name: context-optimization
description: Optimize context usage, reduce token costs, improve context efficiency
user-invocable: true
---
# Context Optimization Skill

Analyze and optimize context usage to reduce token costs and improve efficiency.

## When to Use
- Context is approaching capacity
- Token costs are too high
- Need to compact conversation
- Large refactoring tasks

## Techniques
1. **Compact** — Use `/compact` to summarize and reduce context
2. **Selective Loading** — Only load relevant CLAUDE.md files
3. **Agent Splitting** — Delegate subtasks to subagents
4. **Progressive Disclosure** — Start general, go specific
5. **Memory Management** — Use `/memory` to persist important context

## Context Budget Guidelines
| Usage | Action |
|-------|--------|
| < 50% | Normal operation |
| 50-70% | Start compacting |
| 70-85% | Compact + delegate |
| > 85% | Immediate compact |

## Output
- Current context usage stats
- Optimization recommendations
- Action plan for reducing context
