---
name: pr-summarize
description: Generate a PR summary from git history
---
# PR Summarize Command

Generate a comprehensive PR summary from git commits.

## Steps
1. Run `git log` to get commit history
2. Run `git diff [base-branch]...HEAD` for all changes
3. Analyze the changes
4. Generate a structured summary

## Output Format
- **Title**: Clear, concise PR title
- **Summary**: 2-3 sentence overview
- **Changes**: List of key changes
- **Test Plan**: How to verify the changes
