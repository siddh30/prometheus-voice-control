---
name: jobs
description: Find and rank job postings against the user's resume and stated preferences (LinkedIn and other boards). Use for job search, fit analysis, and shortlist requests.
---
# Job search

1. Load context first: facts from memory (target roles, location, seniority) and the resume (see the `context` skill). If neither exists, ask for the target role once.
2. Search with the `browse` skill, starting at LinkedIn Jobs (`https://www.linkedin.com/jobs/`). Apply filters for role, location, and recency when offered.
3. Open the 5-8 most relevant postings and capture: title, company, location, seniority, key requirements, link.
4. Score each posting against the resume: matching skills, gaps, seniority fit. Give a 1-10 fit score with a one-line reason.
5. Report a ranked shortlist (best first) with links. Offer next steps (tailor resume bullets, draft a note) but do not apply or message anyone without being told to.
6. `remember` the target role/location if the user states them.
