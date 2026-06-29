---
name: lingtai-repo-watch
description: >
  Routinely sweep the entire Lingtai-AI GitHub org for open issues, open PRs,
  draft PRs, and recent activity, then produce one digest the caller can act
  on. Designed to be idempotent and read-only — no triage, labeling, or
  commenting unless the caller explicitly asks for it.

  Reach for this skill when:
    - You are starting a session and want to know "what changed across the
      org since I last looked?" — run the sweep, read the digest.
    - You are a LingTai agent with cycles to spare and want to surface stale
      issues, unreviewed PRs, or repos that have gone quiet.
    - A human asks "what's the state of LingTai right now?" — the digest is
      the most concise answer.

  How to use:
    1. Read this file once — you are learning the routine.
    2. Run the sweep (the `gh` commands below). Default scope: every non-
       archived repo in the Lingtai-AI org. The org list is queried live;
       new repos get picked up automatically.
    3. Render the digest in the section template at the bottom. Group by
       repo, sort by staleness within each section.
    4. Stop. Do not act on findings unless the caller asked for action.
       The digest is the deliverable.
version: 0.1.0
last_changed_at: "2026-05-12T23:30:27-07:00"
---

# LingTai Repo Watch — the Routine

A cross-repo sweep across the entire Lingtai-AI GitHub org. The output is a single markdown digest the caller reads. The skill does not triage, it does not comment, it does not label — it observes and reports. Acting on findings is the caller's call.

## What this skill is for

LingTai is spread across ~14 public + private repos in the Lingtai-AI org (kernel, TUI/portal, MCP servers for telegram/feishu/imap/wechat, plugin repos, skill libraries, agora, libai, homebrew tap). Each repo has its own issues and PRs. Without a periodic sweep, things rot:

- A PR opens against a sibling repo, no one notices for days.
- An issue gets filed by a user, no one assigns or responds.
- A draft PR sits at 90% complete and the author forgets it exists.
- A repo goes quiet for weeks and no one realizes maintenance lapsed.

This skill exists so any caller — you (the human via `/lingtai-repo-watch`) or a LingTai agent on a cron — can answer "what is open across the org right now?" in one pass.

## What this skill is NOT for

- **Not a triage tool.** It does not suggest labels, assign owners, or close stale issues. Surfacing is enough; humans decide.
- **Not a code reviewer.** It lists open PRs but does not review their diffs. (Use `/ultrareview` or the `feature-dev:code-reviewer` agent for that.)
- **Not a metrics dashboard.** No charts, no trends, no week-over-week. One snapshot, current state.
- **Not write-capable.** No comments posted, no labels added, no PRs merged. Read-only.

If the caller wants action, they take it after reading the digest.

## The sweep

The sweep is four `gh` queries plus one synthesis step. Run them in this order — each one composes onto the next. **Run the four data-gathering queries in parallel** (they are independent); then render.

### Step 1 — enumerate live repos

```bash
gh repo list Lingtai-AI --limit 100 --json name,visibility,isArchived,pushedAt,url
```

Filter out archived repos client-side (`isArchived == false`). The result is the canonical list of repos to sweep. **Do not hardcode the list** — the org grows. Note `pushedAt` for the "quiet repos" section later.

### Step 2 — sweep open issues across the org

```bash
gh search issues --owner Lingtai-AI --state open --limit 200 \
  --json number,title,repository,state,createdAt,updatedAt,author,labels,assignees,url
```

`gh search` covers the whole org in one call (cheap). Sort the result by `updatedAt` ascending — oldest-untouched at the top is the highest-signal ordering for triage.

### Step 3 — sweep open PRs across the org

```bash
gh search prs --owner Lingtai-AI --state open --limit 200 \
  --json number,title,repository,state,isDraft,createdAt,updatedAt,author,labels,url,reviewDecision
```

Same shape. Note `isDraft` and `reviewDecision` — these split PRs into actionable groups (ready-for-review, draft-in-progress, changes-requested, approved-but-unmerged).

### Step 4 — recent merged-PR activity (last 7 days)

```bash
gh search prs --owner Lingtai-AI --state closed --merged \
  --merged-at ">$(date -u -v -7d +%Y-%m-%dT%H:%M:%SZ)" \
  --limit 100 \
  --json number,title,repository,mergedAt,author,url
```

(BSD `date` syntax — Linux uses `--date='-7 days'`; the skill is run on macOS and inside agent venvs, so use the BSD form. If running in a Linux container, swap to `date -u -d '7 days ago' +%Y-%m-%dT%H:%M:%SZ`.)

This anchors the digest in time — the reader sees both what's open and what shipped recently.

### Step 5 — render

Compose the digest using the template in **Output format** below. Do not invent sections; do not omit empty sections — render them as `(none)` so the reader knows the sweep happened and found nothing, distinct from the sweep silently skipping that group.

## Output format

The digest is one markdown document. Every sweep produces this exact shape, even when sections are empty:

```markdown
# LingTai org sweep — <UTC timestamp>

Scope: <N> repos (<M> public, <K> private), <total> open issues, <total> open PRs.

## Open PRs needing attention

Group by status — within each group, sort by `updatedAt` ascending (oldest first, since stale matters more than fresh). One line per PR:

`<repo>#<num>` — <title> · <author> · opened <relative date> · last update <relative date> · <reviewDecision or "no review yet">

### Ready for review (not draft, no approval yet)
- ...

### Changes requested
- ...

### Approved but not merged
- ...

### Drafts (in progress)
- ...

## Open issues

Group by repo. Sort issues within a repo by `updatedAt` ascending. For each repo with open issues:

### <repo> (<count> open)
- `#<num>` — <title> · <labels if any> · opened <relative> · last touched <relative> · <assignee or "unassigned">

Skip repos with zero open issues. If ALL repos have zero open issues, render the section as `(no open issues across the org)`.

## Recently merged (last 7 days)

One line per merged PR, grouped by repo, sorted by `mergedAt` descending (newest first):

### <repo>
- `#<num>` — <title> · <author> · merged <relative date>

If empty: `(no PRs merged in the last 7 days)`.

## Quiet repos

Repos with no commit activity in 14+ days (`pushedAt` from Step 1). One line each:

- `<repo>` — last push <relative date> ago · <visibility>

If none: `(all repos active in the last 14 days)`.

## Summary

Three lines max. Examples of what belongs:
- "5 PRs ready for review across kernel and TUI; nothing critical."
- "3 repos quiet >30d (homebrew-lingtai, libai-comments, codex-plugin) — likely fine, low-change repos."
- "1 issue with no assignee filed by external user 6 days ago: lingtai-telegram#12 — recommend triage."
```

## Conventions and gotchas

- **Relative dates only in the digest.** "3 days ago" reads better than `2026-05-04T10:33:21Z`. Compute relative against the sweep timestamp at the top of the digest. ISO timestamps belong in tool output, not human-facing summaries.
- **`gh search` API quirks.** `gh search prs --owner` returns PRs; `gh search issues --owner` returns ONLY issues (PRs are excluded automatically). This split is exactly what you want — don't try to merge them.
- **The 200-result cap.** `--limit 200` is the gh CLI cap for `search`. Across ~14 repos it has historically been more than enough. If a sweep returns exactly 200 in a category, suspect saturation and note it in the Summary.
- **Private repos.** The current Lingtai-AI org has at least one private repo (`libai-web`). The sweep will include it if the caller's `gh` auth has access; if not, it silently disappears from the result. Note repo count in the header so the caller can spot if private repos are missing.
- **Archived repos are excluded by default.** They cannot have new issues; including them would be noise. Filter on `isArchived == false` in Step 1.
- **No issue body, no PR diff.** The digest carries titles only. Anyone who wants to dig in clicks the URL. This keeps the digest scannable — typically <300 lines for the whole org.
- **Idempotent.** Running the sweep twice in a row produces nearly identical output (only relative timestamps shift). Safe to run on a cron without state.
- **Time zone.** All timestamps in the digest header are UTC. Relative phrasing ("3 days ago") is timezone-neutral so the reader's TZ doesn't matter.

## Calling the skill

For a human in Claude Code, the wrapper slash command (`~/.claude/commands/lingtai-repo-watch.md`) reads this SKILL.md and runs the sweep. The slash command is the trigger; this file is the routine. Edit this file and the next slash invocation picks up the change.

For a LingTai agent, the skill is auto-installed at every agent boot under `.library/intrinsic/capabilities/lingtai-repo-watch/`. An agent can read it from there and run the sweep on its own cadence (e.g. once at start of day, then every few hours).

## Maintenance

When you make changes that affect the sweep — a new repo type, a new query, a new section — update this file in the same change. The slash command and any agents read the SKILL.md verbatim; there is no second source of truth.

When you add a new repo to the Lingtai-AI org, do nothing — the skill picks it up automatically via Step 1's `gh repo list`.

When the gh CLI changes its query syntax (rare but it has happened), update the four `gh` invocations and bump `version` in the frontmatter so callers can tell the routine has shifted.
