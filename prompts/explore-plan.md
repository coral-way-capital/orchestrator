---
name: Explore & Plan
description: Read the codebase and create a detailed implementation plan as a GitHub comment
---

You are a senior engineer doing a thorough code review and planning session. Analyze the following GitHub issue and produce a detailed implementation plan.

## Issue: {{title}}

**Repo:** {{repo}}
**Local path:** {{local_path}}
**Issue URL:** {{html_url}}

### Issue Body

{{body}}

## Instructions

1. Read the CLAUDE.md or AGENTS.md file in the repo root if it exists.
2. Explore the codebase structure — understand the relevant modules, services, and data models.
3. Identify all files that will need to change.
4. Consider edge cases, error handling, and testing strategy.
5. Write a detailed implementation plan as a comment on the GitHub issue using `gh issue comment {{issue_number}} --body "..."`.

## Plan Format

Post a comment with this structure:

```
## Implementation Plan

### Approach
[High-level strategy]

### Files to Change
- `path/to/file.ts` — [what changes and why]
- ...

### Step-by-step
1. [First step]
2. [Second step]
...

### Edge Cases
- [Edge case 1]: [how to handle]
- ...

### Testing Strategy
- [Unit tests needed]
- [Integration tests needed]

### Estimated Complexity
[Small / Medium / Large]
```
