# Rastro health

Rules of `d27aa39`; process and pipeline figures cover the last 30 days.

> **Warning:** 2 commit(s) were not analysed because of the cost budget or the model quota: the numbers below may be out of date.

## Knowledge base

| Figure | Value | Note |
| --- | --- | --- |
| Active rules | 27 | 1 orphaned, 2 rejected |
| With a known reason | 19 of 27 (70%) |  |
| Confirmed by a person | 3 of 27 (11%) |  |
| ...verified by GitHub | 1 of 3 (33%) - small sample |  |
| ...by someone other than the PR author | 1 of 2 (50%) - small sample |  |
| Confirmation outdated by edits | 1 of 4 (25%) - small sample |  |
| Oldest unconfirmed rule | 40 days (median 25) | 24 rule(s) waiting |

## Process (last 30 days)

| Figure | Value |
| --- | --- |
| Pull requests with proposals | 4 |
| Reviewed before the merge | 3 of 4 (75%) - small sample |
| Integrated without review | 1 of 4 (25%) - small sample |
| Time to first review | n/a (fewer than 3 distinct reviewers: a breakdown could point at a person) |
| Verdicts | 5 of 8 (62%) - small sample confirmed as written, 2 corrected, 1 rejected |

## Agents (last 30 days)

| Figure | Value |
| --- | --- |
| Files with rules announced first | 9 of 15 (60%) |
| Sessions where the hook ran | 7 of 11 (64%) |

## Code without recorded intent (last 30 days)

| Figure | Value |
| --- | --- |
| Integrations with no session | 6 of 14 (43%) |

40 commit(s) with no session and no pull request are left out.

## Zones

| Zone | Symbols | With a rule | Rules | Confirmed | No reason |
| --- | --- | --- | --- | --- | --- |
| src/domain | 40 | 12 | 10 | 3 | 5 |
| src/api | 0 | 0 | 0 | 0 | 0 |

## Review queue

| # | Rule | Why | Do |
| --- | --- | --- | --- |
| 1 | Each client may have at most 3 active appointments at the same time | no reason recorded; not confirmed by a person; 7 caller(s) | `rastro rule 2a7851 --confirm` |
