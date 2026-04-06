---
name: code-review
description: Run code review on current uncommitted changes
---
# Code Review Command

Review uncommitted changes using the code-reviewer agent.

## Usage
```
/code-review
```

## Process

1. **Run git diff** to see all uncommitted changes
2. **Analyze each file** for:
   - Correctness
   - Readability
   - Best practices
   - Security issues
3. **Invoke code-reviewer agent** with findings
4. **Present structured feedback**

## What to Review

### All Changes
- New files added
- Modified files
- Deleted files

### Focus Areas
- Business logic changes
- Security-sensitive code (auth, payments, data handling)
- API contracts
- Database migrations

## Output

Present review with:
- Summary (overall assessment)
- CRITICAL issues (must fix)
- HIGH issues (should fix)
- MEDIUM issues (consider fixing)
- Suggestions (optional improvements)

## After Review

User should:
1. Address CRITICAL issues
2. Fix HIGH issues before merge
3. Consider MEDIUM issues
4. Apply suggestions if time permits
