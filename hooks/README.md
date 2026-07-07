# Git hooks

Tracked hooks for this repo. Enable them once per clone:

    git config core.hooksPath hooks

(`core.hooksPath` is local config, not committed, so a fresh clone must run the
line above once.)

## pre-commit

Auto-bumps the **patch** version in `src/financial_agent/__init__.py` on any
commit that touches `src/financial_agent/`. It re-stages the file into the same
commit.

- Commits that only touch docs/tests/tooling do **not** bump.
- To land a **minor** or **major** release, edit `__version__` yourself in the
  same commit; the hook sees the changed version line and steps aside.

Message-driven bumping (feat -> minor automatically) is intentionally not done:
only a `pre-commit` hook can add a file to the current commit, and it has no
access to the commit message. Reserve minor/major for a manual edit or a release
step.
