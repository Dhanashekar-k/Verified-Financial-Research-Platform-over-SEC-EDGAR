# Contributing & team workflow

This repo is built solo but run as if a large team shared it. The point is to
practice the real mechanics: nothing reaches `main` except through a reviewed,
CI-gated pull request.

## Branching (trunk-based)
- `main` is always releasable and protected. No direct pushes.
- Short-lived branches off `main`, named `<type>/<milestone>-<slug>`:
  `feat/m1-xbrl-lakehouse`, `fix/m5-period-dedup`, `chore/ci-cache`, `docs/adr-007`.
- Rebase on `main` before opening the PR; keep branches no more than a few days old.

## Commits (Conventional Commits)
- `type(scope): imperative subject <= 72 chars`
- types: feat, fix, chore, docs, test, refactor, perf, ci
- Body says *why*, not what. One concern per commit.

## Pull requests
1. Open the PR early (draft is fine). Link the issue: `Closes #NN`.
2. CI (`lint-and-test`) must be green.
3. CODEOWNERS review is auto-requested and must approve.
4. Resolve every conversation, squash-merge, delete the branch.

## The solo-simulation caveat (be honest about this)
GitHub won't let you approve your own PR, and a real 100-person flow has *someone
else* gate your merge. Pick one and write down which:
- **Self-review discipline (recommended):** add real review comments to your own PR
  as if you were the reviewer — hunt for the items in the PR checklist — then
  admin-merge. You get the review muscle; you just hold both roles.
- **Second account / a friend:** add a collaborator who actually clicks Approve.
Don't pretend a rubber-stamp was a review.

## What never goes in a PR
Invented numbers (CLAUDE.md rule 1), edits to gold eval data, `.env` or secrets,
`terraform apply`, or a contract change without the data-contract doc + an ADR.
