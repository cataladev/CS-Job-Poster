# Job footer source codes

Individual Discord job posts append compact source codes after the existing delivery ID:

```text
Job Bot • jobbot:53c4d9de85fa970a12b6 • JB-13
```

The footer uses these codes without spelling out the feed names. This document is
the developer lookup.
The implementation lives in [`job_bot/source_codes.py`](job_bot/source_codes.py).

## Mappings

GitHub codes follow the original feed-list order. Codes identify a logical feed
and stay the same when its repository moves to a new recruiting year.

| Code | Internal source ID | Feed | Current repository / surface |
|---|---|---|---|
| `GH-1` | `simplify_internships` | Simplify summer internships | `SimplifyJobs/Summer2027-Internships` · `dev/README.md` |
| `GH-2` | `simplify_new_grad` | Simplify new grad | `SimplifyJobs/New-Grad-Positions` · `dev/README.md` |
| `GH-3` | `simplify_offseason_internships` | Simplify off-season internships | `SimplifyJobs/Summer2027-Internships` · `dev/README-Off-Season.md` |
| `GH-4` | `speedy_internships` | SpeedyApply internships | `speedyapply/2027-SWE-College-Jobs` · `main/README.md` |
| `GH-5` | `speedy_new_grad` | SpeedyApply new grad | `speedyapply/2027-SWE-College-Jobs` · `main/NEW_GRAD_USA.md` |
| `GH-6` | `sndsh_internships` | sndsh404 summer internships | `sndsh404/summer-2027-internships` · `main/README.md` |
| `GH-7` | `intern_engine` | Internship Engine JSON | `zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships` · `main/docs/api/jobs.json` |
| `GH-8` | `intern_engine_readme` | Internship Engine README | `zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships` · `main/README.md` |
| `GH-9` | `applyguy_internships` | ApplyGuy internships | `ApplyGuy/2027-Internships` · `main/data/internships.json` |
| `GH-10` | `applyguy_new_grad` | ApplyGuy new grad | `ApplyGuy/2027-New-Grad-Jobs` · `main/data/new-grad-jobs.json` |
| `JB-13` | `jobright_internships`, `jobright_new_grad` | Jobright, both tracks | Anonymous visitor API, including the SSR listing fallback |
| `DS-1` | `direct_*`, `company_api_*` | Direct employer scrapers, both tracks | Public ATS boards and first-party company APIs, including `company_api_ibm` |

`DS-1` groups all direct scrapers. Current ATS source IDs use
`direct_<provider>_<board hash>` for Greenhouse, Lever, Ashby, SmartRecruiters,
Workday, Oracle, Optiver, ByteDance, Rippling, and Amazon. First-party company
adapters use `company_api_<company>`; IBM is currently registered.

## Posting behavior

- Codes come from the canonical job's saved ingestion source IDs. An employer
  application URL resolved for a Jobright listing still receives `JB-13`.
- When multiple sources have found the same job before delivery, the footer lists
  every distinct code, sorted by prefix and then number. For example:
  `Job Bot • jobbot:53c4d9de85fa970a12b6 • DS-1, GH-2, GH-10, JB-13`.
- Multiple observations sharing a code produce only one copy of that code.
- Unknown source IDs are omitted without displaying their internal names. If none
  have a mapping, the footer retains the original `Job Bot • jobbot:<id>` format.
- The delivery ID stays unchanged, so retry reconciliation works with both old
  and new footers. Category-thread copies preserve the same footer.
- This applies to newly sent individual job posts. Previously sent posts are not
  edited, and daily digest footers retain their existing format.

## Adding a feed

Assign each new GitHub list the next unused `GH-<number>` in `SOURCE_CODES` and add
its row here. Never renumber or reuse an existing code. Direct scrapers following
the `direct_` or `company_api_` source-ID conventions automatically use `DS-1`.
Add an explicit mapping and document it here for any new source family.
