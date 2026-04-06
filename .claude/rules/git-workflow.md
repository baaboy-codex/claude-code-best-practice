# Git Workflow Rules

## Commit Message Format

```
<type>: <description>

<optional body>
```

**Types:**
- `feat`: New feature
- `fix`: Bug fix
- `refactor`: Code restructure (no behavior change)
- `docs`: Documentation only
- `test`: Tests only
- `chore`: Maintenance tasks
- `perf`: Performance improvement
- `ci`: CI/CD changes

## Commit Rules

1. **Separate commits per file** — Do NOT bundle multiple file changes into one commit
2. **Use imperative mood** — "Add feature" not "Added feature"
3. **First line under 72 characters**
4. **Reference issues when applicable** — "fix #123: ..."

## Example

```
feat: add user authentication

Implement JWT-based authentication for API endpoints.
Add login/logout commands.

Closes #45
```

## PR Process

1. Analyze full commit history with `git log`
2. Review all changes with `git diff [base]...HEAD`
3. Write comprehensive PR description
4. Include test plan
5. Request appropriate reviewers
