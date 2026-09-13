# Git Workflow

This project is connected to a GitHub repository.

You are responsible for maintaining a clean Git history.

## Before making changes
- Check `git status`.
- Inspect the current branch.
- Do not overwrite or discard existing user changes unless explicitly instructed.

## After significant changes
When you complete a meaningful unit of work:
1. Run the relevant tests/build/linter.
2. Check `git diff`.
3. Review the files that changed.
4. Stage only the relevant files.
5. Create a descriptive commit.
6. Push the commit to the GitHub remote.

Use commit messages that explain what changed, for example:
- `feat: add user authentication`
- `feat: add project dashboard`
- `fix: handle expired sessions`
- `refactor: simplify API client`
- `test: add authentication tests`

## What counts as a significant change
Push after completing a meaningful feature, bug fix, refactor, database/schema change, API change, UI section, or other substantial milestone.

Do NOT create a commit for every tiny edit.

## Before pushing
Always verify:
- No secrets/API keys are being committed.
- `.env` and other sensitive files remain ignored.
- Tests/build pass when applicable.
- The working tree contains only intended changes.

Never use `git push --force` unless explicitly instructed by the user.
Never commit secrets, credentials, API keys, tokens, or private configuration.