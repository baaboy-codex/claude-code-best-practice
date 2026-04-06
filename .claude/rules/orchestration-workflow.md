# Orchestration Workflow Pattern

## Command → Agent → Skill Architecture

The recommended workflow pattern for complex tasks in Claude Code.

```
╔══════════════════════════════════════════════════════════════════╗
║           ORCHESTRATION WORKFLOW                               ║
║           Command → Agent → Skill                              ║
╚══════════════════════════════════════════════════════════════════╝

                         ┌───────────────────┐
                         │  User Interaction │
                         └─────────┬─────────┘
                                   │
                                   ▼
         ┌─────────────────────────────────────────────────────┐
         │  /command — Command (Entry Point)                   │
         │  - Handles user interaction                          │
         │  - Orchestrates workflow                             │
         │  - Uses Agent tool for data fetching                │
         │  - Uses Skill tool for output creation              │
         └─────────────────────────┬───────────────────────────┘
                                   │
                              Step 1: Agent
                                   │
                                   ▼
         ┌─────────────────────────────────────────────────────┐
         │  agent — Agent ● skill: preloaded-skill              │
         │  - Fetches data using preloaded skill                │
         │  - Returns structured data to command                │
         │  - Agent skill pattern: skills: [skill-name]         │
         └─────────────────────────┬───────────────────────────┘
                                   │
                              Step 2: Skill
                                   ▼
         ┌─────────────────────────────────────────────────────┐
         │  skill — Skill (Independent)                        │
         │  - Creates output from command context              │
         │  - Invoked via Skill tool                           │
         │  - Receives data through conversation               │
         └─────────────────────────────────────────────────────┘
```

## Two Skill Patterns

| Pattern | Invocation | Preloading | Use Case |
|---------|-----------|------------|----------|
| **Agent Skill** | Preloaded via `skills:` | Injected at startup | Domain knowledge for agents |
| **Skill** | Via `Skill()` tool | Not preloaded | Standalone operations |

## Example: Book Summary Pipeline

```
/book-summary
    │
    ├─ Step 1: Agent tool → book-agent
    │   └─ Preloaded: book-fetcher
    │   └─ Returns: {title, author, chapters}
    │
    └─ Step 2: Skill tool → book-summary-creator
        └─ Creates: summary.md
```

## When to Use This Pattern

| Task Type | Approach |
|-----------|----------|
| Simple edit/query | Direct execution |
| Multi-step with data fetch | Command → Agent → Skill |
| Complex orchestration | Command → multiple Agents → multiple Skills |

## Frontmatter Examples

### Command
```yaml
---
name: my-command
description: Description of what it does
argument-hint: [param1] [param2]
---
```

### Agent with Agent Skill
```yaml
---
name: my-agent
skills:
  - my-domain-skill    # Preloaded at startup
tools: Read, Bash
model: sonnet
---
```

### Independent Skill
```yaml
---
name: my-output-skill
description: Creates output files
---
```
