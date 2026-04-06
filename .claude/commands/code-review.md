---
name: code-review
description: Run code review on current changes
---
# Code Review Command

Review the current uncommitted changes for quality and best practices.

## Steps
1. Run `git diff` to see all uncommitted changes
2. Analyze each changed file
3. Invoke the code-reviewer agent with findings
4. Present structured feedback to user

## Output
- Summary of changes
- CRITICAL/HIGH/MEDIUM issues
- Actionable suggestions
