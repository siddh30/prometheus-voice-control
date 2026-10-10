---
name: context
description: Find and read the user's local files for context (resume, notes, project docs) to personalize searches and answers.
---
# Local context

1. The folders you may search are listed in the system prompt under Local context folders. Stay inside them.
2. For a resume, `Glob` for `*resume*`, `*cv*` (pdf, docx, md, txt) and pick the most recently modified. Read it with `Read` (PDFs are supported).
3. Distill it into a compact profile (roles, years, skills, industries, location) and `remember` it under key `resume_summary` so later sessions do not re-read the file.
4. Never quote private details beyond what the task needs.
