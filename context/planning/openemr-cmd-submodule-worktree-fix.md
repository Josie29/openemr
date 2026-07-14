# `openemr-cmd worktree` submodule fix

`openemr-cmd` is a personal tool (`~/bin/openemr-cmd`, upstream in `openemr/openemr`),
not vendored in this repo — so a reinstall/self-upgrade drops local edits. This
doc records the one-block patch that makes `worktree add`/`--start` work in this
fork so it can be re-applied.

## Symptom

`openemr-cmd worktree add …` (and `--start`) died with:

```
Compose directory not found: <path>/docker/development-easy
```

## Root cause

This repo is a **git submodule** of the parent `gauntlet-ai` repo, so its `.git`
is a *file* and `git rev-parse --git-common-dir` returns
`…/gauntlet-ai/.git/modules/projects/project-01-agentforge`.

The old `OPENEMR_ROOT` resolver assumed the linked-worktree-of-a-normal-repo
layout: it took `dirname(common-dir)` and trusted it when it differed from
`show-toplevel`. Under the submodule that `dirname` lands **inside `.git`**
(`…/.git/modules/projects`), which it wrongly adopted as the repo root — so
`$OPENEMR_ROOT/docker/development-easy` didn't exist.

The signal it missed: for a submodule *primary* checkout `git-dir == common-dir`
(not a linked worktree at all), and the canonical pointer to the real working
tree is the module gitdir's `core.worktree`.

## The fix (`~/bin/openemr-cmd`, the `OPENEMR_ROOT=…` block near the top)

Replace the `dirname(common-dir)`-vs-`show-toplevel` heuristic with a
`core.worktree`-aware resolver that handles all four layouts (normal/submodule ×
primary/linked-worktree):

```bash
OPENEMR_ROOT="${OPENEMR_ROOT:-$(
    git rev-parse --show-toplevel >/dev/null 2>&1 || { echo "${HOME}/dev/openemr"; exit 0; }
    # The common gitdir's config carries core.worktree iff the working tree is
    # detached from the gitdir (the submodule case) — it points at the primary
    # checkout relative to the gitdir. Otherwise the primary tree is the .git
    # dir's parent.
    _common=$(realpath "$(git rev-parse --git-common-dir 2>/dev/null)")
    _cw=$(git config -f "${_common}/config" core.worktree 2>/dev/null || true)
    if [[ -n "${_cw}" ]]; then
        realpath "${_common}/${_cw}"
    else
        dirname "${_common}"
    fi
)}"
```

Backward-compatible: a normal repo has no `core.worktree`, so it falls through to
`dirname(common-dir)` = the old behaviour. No other part of `openemr-cmd` needed
changing — the git bind-mount into the worktree container (`primary_git_real`)
was already submodule-aware, and `git worktree list` reports *linked* worktrees
correctly (only the submodule *main* worktree is misreported, which
`wt_validate_dir` never touches).

## Verified

`worktree add … --start` boots the full stack under the submodule; in-container
git resolves (`git rev-parse --is-inside-work-tree` → true), so husky/prek/
composer hooks work. See the "Working in a git worktree" section of `CLAUDE.md`
for the end-to-end Co-Pilot full-stack workflow that builds on this.
