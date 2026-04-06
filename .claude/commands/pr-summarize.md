---
name: pr-summarize
description: Generate a PR summary from git history
argument-hint: [base-branch]
---
# PR Summarize Command

Generate a comprehensive PR summary from git commits and diff.

## Usage
```
/pr-summarize
/pr-summarize main
/pr-summarize origin/develop
```

## Process

1. **Get commit history**
   ```bash
   git log [base-branch]..HEAD --oneline
   ```

2. **Get full diff**
   ```bash
   git diff [base-branch]...HEAD
   ```

3. **Analyze changes**
   - Categorize by type (feat, fix, refactor, etc.)
   - Identify key changes
   - Note breaking changes

4. **Generate summary**

## Output Format

```markdown
# PR Title: [Clear, concise title]

## Summary
[2-3 sentence overview of what this PR does and why]

## Type
- [ ] Feature
- [ ] Bug Fix
- [ ] Refactor
- [ ] Documentation
- [ ] Performance
- [ ] Other

## Changes

### Added
- [List of new files/features]

### Modified
- [List of changed files with key changes]

### Removed
- [List of deleted files/features]

## Breaking Changes
[None / List breaking changes]

## Test Plan
- [ ] Unit tests added/updated
- [ ] Integration tests pass
- [ ] Manual testing steps (if needed)

## Screenshots/Artifacts
[Any relevant screenshots, recordings, or artifacts]

## Related Issues
Closes #[number]
```

## Guidelines
- Title should be under 72 characters
- Summary should answer "what" and "why"
- Include migration steps for breaking changes
- Provide steps to verify the changes
