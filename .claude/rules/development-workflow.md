# Development Workflow Rules

## Feature Implementation Workflow

0. **Research & Reuse** (mandatory before any new implementation)
   - Search GitHub for existing implementations
   - Check library documentation
   - Search package registries (npm, PyPI, crates.io)

1. **Plan First**
   - Use `planner` agent for complex features
   - Generate planning docs: architecture, task list
   - Identify dependencies and risks

2. **TDD Approach**
   - Write failing tests first (RED)
   - Implement minimal code (GREEN)
   - Refactor (IMPROVE)
   - Target 80%+ coverage

3. **Code Review**
   - Use `code-reviewer` agent
   - Address CRITICAL and HIGH issues
   - Fix MEDIUM when possible

4. **Security Review**
   - Use `security-reviewer` agent for sensitive changes
   - Check for OWASP top 10 issues

5. **Commit & Push**
   - Separate commits per file
   - Follow conventional commits format

## When to Use Subagents

| Task Type | Use |
|-----------|-----|
| Simple edit | Main agent |
| Code review | `code-reviewer` agent |
| Security analysis | `security-reviewer` agent |
| Complex feature | `planner` → implementation |
| New feature with tests | `tdd-guide` agent |

## Context Management

- `/compact` at ~50% context usage
- Break tasks that exceed 50% context
- Keep CLAUDE.md under 200 lines
- Use `haiku` for lightweight tasks
- Use `sonnet` for main development
- Use `opus` for architectural decisions
