---
name: generate-pr
description: Generate a PR title and description from the current branch's commits, then create the PR via gh
user-invocable: true
---

# Generate Pull Request

Automatically generate a PR title and description from the current branch's changes, then create it on GitHub.

## Steps

### 1. Gather context

Run these commands to understand the branch:

```
git branch --show-current
git log main..HEAD --oneline
git diff main..HEAD --stat
git diff main..HEAD
```

If the branch has no commits ahead of main, tell the user there's nothing to PR.

### 2. Generate the PR title

- Under 70 characters
- Use conventional style: `Add ...`, `Fix ...`, `Update ...`, `Remove ...`
- Describe the user-facing change, not implementation details

### 3. Generate the PR description

Use this format:

```markdown
## Summary
- Bullet point 1 (what changed and why)
- Bullet point 2
- ...

## Changes
- `path/to/file.py` -- what changed in this file
- `path/to/other.py` -- what changed here
- ...

## Test plan
- [ ] Tests pass (`make test` or `pytest`)
- [ ] Any additional verification steps specific to these changes
```

Keep it concise. Focus on *what* and *why*, not *how*. Group related file changes together.

### 4. Create the PR

Use `gh pr create` to create the PR directly:

```
gh pr create --title "<title>" --body "<description>"
```

Use a HEREDOC for the body to preserve formatting. Target `main` by default.

If `gh` is not available or not authenticated, output the title and description as markdown so the user can copy-paste.

### 5. Report

Print the PR URL when done.
