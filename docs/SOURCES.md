# Feed Sources

## GitHub Repository Feeds

Parsed from markdown tables or JSON files in public GitHub repos. Polled every 5 minutes via conditional requests (ETag/Last-Modified). Each feed has a stable source code (e.g. `GH-1` through `GH-10`) shown in Discord footers — see `SOURCE_CODES.md` for the full mapping.

| Source ID | Label | Repository | File | Track |
|-----------|-------|-----------|------|-------|
| `simplify_internships` | Simplify | SimplifyJobs/Summer2027-Internships | README.md | internship |
| `simplify_new_grad` | Simplify | SimplifyJobs/New-Grad-Positions | README.md | new_grad |
| `simplify_offseason_internships` | Simplify | SimplifyJobs/Summer2027-Internships | README-Off-Season.md | internship |
| `speedy_internships` | SpeedyApply | speedyapply/2027-SWE-College-Jobs | README.md | internship |
| `speedy_new_grad` | SpeedyApply | speedyapply/2027-SWE-College-Jobs | NEW_GRAD_USA.md | new_grad |
| `sndsh_internships` | Summer 2027 list | sndsh404/summer-2027-internships | README.md | internship |
| `intern_engine` | Internship Engine | zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships | docs/api/jobs.json | internship |
| `intern_engine_readme` | Internship Engine | zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships | README.md | internship |
| `applyguy_internships` | ApplyGuy | ApplyGuy/2027-Internships | data/internships.json | internship |
| `applyguy_new_grad` | ApplyGuy | ApplyGuy/2027-New-Grad-Jobs | data/new-grad-jobs.json | new_grad |

## Jobright

Live job aggregator API. Polled every 60 seconds. Uses the visitor taxonomy window endpoint for computer-science intern/new-grad roles (30-card window per sort condition). Falls back to listing page scrape on API failure.

| Source ID | Track | Taxonomy |
|-----------|-------|----------|
| `jobright_internships` | internship | CS intern seniority band |
| `jobright_new_grad` | new_grad | CS new-grad seniority band |

Features: ISP proxy rotation, exponential backoff, employer link resolution (replaces Jobright wrapper URLs with direct employer links when a session cookie is configured).

When link resolution fails for eight attempts in a row it suspends itself and posts one alert to `JOBRIGHT_STATUS_WEBHOOK_URL` naming the cause -- `session_rejected`, `interstitial` (a security check instead of Jobright's page), `blocked`, `throttled`, `proxy_error`, `timeout`, `upstream_error`, `unreadable_payload` -- with the HTTP status, the egress it used, and the error itself. Only `session_rejected` asks for a new `JOBRIGHT_SESSION_ID`.

Transport: every Jobright request -- window, listing fallback, and link resolution -- leaves through a Chrome-impersonating curl_cffi client (`ImpersonatingHttpClient`) rather than the shared aiohttp client the other feeds use, because Jobright's edge screens the TLS/HTTP2 handshake before it reads a header. The ISP proxy pool applies to that client per request, so egress rotation is unchanged.

## Company API Sources

First-party career site APIs. Polled on the company source timer (separate from core feeds). Only deliver jobs with a same-day `posted_date`.

| Company | Source ID | API Endpoint | Seniority Filters | Tracks |
|---------|-----------|-------------|-------------------|--------|
| IBM | `company_api_ibm` | `https://www-api.ibm.com/search/api/v2` | Internship, Entry Level | internship, new_grad |
| Microsoft | `company_api_microsoft` | `https://apply.careers.microsoft.com/api/pcsx/search` | Intern, Recent Graduate | internship, new_grad |

IBM uses a POST JSON search API with career area filters (Software Engineering, Infrastructure & Technology, Data & Analytics, Cloud).

Microsoft uses a GET API with `filter_seniority` params, paginated (20 per page, up to 200 results per seniority level), sorted by timestamp.

## Direct ATS Board Feeds

Public job board APIs queried when a Jobright observation names a matching employer. Built from `data/companies.csv` seed + curated boards in `ats_feeds.py`. Subject to a 4-hour cache cooldown per board.

### Supported ATS Platforms

| Platform | API Style | Example Employers |
|----------|-----------|-------------------|
| Greenhouse | GET JSON board API | Anduril, Figma, Jump Trading, IMC, Appian, Nuro, Astranis |
| Lever | GET JSON postings API | (from seed CSV) |
| Ashby | GET JSON board API | Abridge, Exa, Neptune.io, Creatify AI |
| SmartRecruiters | GET JSON with pagination | (from seed CSV) |
| Workday | POST CXS endpoint (20/page, 5 pages max) | (from seed CSV) |
| Oracle | GET JSON | (from seed CSV) |
| Optiver | Custom JSON endpoint | Optiver |
| ByteDance | POST search API | ByteDance |
| Rippling | POST Algolia search | Rippling |
| Amazon | GET search API | Amazon |

### Curated Boards

Hardcoded in `ats_feeds.py` as `CURATED_DIRECT_ATS_BOARDS`. These are high-signal employers with confirmed public board URLs that supplement the seed CSV.
