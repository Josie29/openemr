# Workflow / Product-Owner Tooling Landscape

**Purpose:** Survey of project-management tools that support the "assign a ticket to an
AI coding agent" pattern, evaluated against this project's actual shape — a one-week,
GitHub-native sprint (per `../PRD-week-1.md`, deadline 2026-07-12), already running Claude Code
against a git-worktree-per-branch workflow (`../CLAUDE.md` § Working in a git worktree),
with a real near-term need: parallel work on the PHP frontend module and the Python
`agent/` backend. Not decision evidence for a PRD deliverable — this is tooling choice for
how the team runs, not what gets shipped.

**Grounding:** pricing and agent-assignment capability verified via live research as of
2026-07-08 (not relying on training-data priors — several of these tools changed pricing
models or shut down in the last year; see callouts below).

---

## The landscape

| Tool | Free tier | Cheapest paid tier | Agent-assignment capability |
|---|---|---|---|
| **GitHub Issues + Projects + Copilot** | Free tier exists (unlimited private repos) | Copilot Pro $10/mo | Assign an Issue to **Copilot** as assignee → it opens a draft PR, posts a checklist, pushes commits, iterates on review. Confirmed working today, not gated to a higher tier. Billing shifted June 2026 from premium-request-units to token-based AI Credits. |
| **Linear** | 2 teams, 250-issue cap (agents bundled even on Free) | Basic $10/user/mo (Business $16/user/mo for full agent roster) | Native "Agents" assignee slot — works out of the box with Cursor, Codex, and Devin. **No official Claude Code integration** (open issue `anthropics/claude-code#12925`); workaround only via a community runner or Linear's MCP server. |
| **Task Master AI** | Fully free (MIT + Commons Clause — can't resell as a hosted service) | N/A | Parses a PRD into a dependency-tracked task graph, exposes an **MCP server** Claude Code/Cursor/Windsurf can query directly. Planning/bookkeeping only — does not itself dispatch or execute agents. |
| **Shortcut** | Free, capped at 10 users | Team $8.50/user/mo | New (Sep 2025) **"Korey"** agent — assignable to a Story like a teammate; writes acceptance criteria and decomposes sub-tasks. Acts as an AI *product manager*, not a PR-opening coding agent. |
| **Plane** (OSS, self-hostable) | Free cloud tier; Community Edition free/self-hosted (AGPL v3) | Cloud Pro ~$6–7/seat/mo | Repositioned as "agent-native" — ships **Plane Agents** (engine "Pi") reacting to workspace events, auto-assigning owners, scaffolding subtasks, plus an Agent Dev Kit for custom agents. Substantial shift from its old "OSS Linear clone" reputation. |
| **Jira** | ≤10 users, 2GB storage | Standard ~$7.91/user/mo | No native "assign → autonomous PR" pattern. **Rovo Dev is a separate credit-metered add-on** (2,000 credits/user/mo included, $0.01/credit overage) — closer to a CLI/PR-review assistant than an issue-assignee agent. |
| **ClickUp** | Free, unlimited tasks, 60MB storage | Unlimited $7/user/mo | AI is a **separate paid add-on**, not bundled — Brain $9/user/mo or "Everything AI" $28/user/mo on top of the seat price. Assistant/automation-flavored, not a clean agent-assignee pattern. |
| **Notion** | Free, unlimited blocks | Plus $10/user/mo (annual) | AI pricing restructured Sep 2025 — full Notion AI/Agents now bundled **only into Business** ($20/user/mo annual). Its "Agents" do multi-step workspace automation (pages/databases), not coding-agent PR work. |
| ~~Height~~ | — | — | **Defunct — shut down permanently Sep 24, 2025, all data deleted.** Was the "autonomous AI PM" pioneer; didn't survive commercially. Excluded from consideration. |

**What's changed vs. 2024–2025-era assumptions, worth flagging explicitly:**
- Height is gone. Drop it from any planning that assumes it's still around.
- Claude Code is, ironically, the one major coding agent *without* native Linear support — the tool otherwise leading on agent-assignee UX doesn't cover the agent this project is built with.
- Shortcut and Plane both leapt into agentic AI in the last year — no longer "bare manual trackers."
- Notion and GitHub Copilot both restructured AI billing in the last year (Notion: unbundled add-on → tier-gated; Copilot: premium-request units → token credits).
- ClickUp keeps AI unbundled and comparatively expensive on top of seat price, unlike Linear which bundles agent support even into its cheapest paid tier.

---

## Recommendation

**Use what you already have: GitHub Issues/Projects + GitHub Copilot Pro ($10/mo), plus Task Master AI (free) as the planning layer. Skip a dedicated PM tool for this sprint.**

Reasoning, mapped to your actual situation:

1. **Your parallel-branch plan is the exact use case Copilot's agent-assignee already covers.** You're using `openemr-cmd worktree` to run the PHP frontend and the Python `agent/` backend in tandem. Rather than introducing a second tool to coordinate that, open a GitHub Issue per frontend task in the worktree's branch context and assign it to **Copilot** — it opens its own PR, and you keep driving the backend directly through this Claude Code session. Two agents, two branches, zero new surface area, $10/mo.

2. **Linear's core selling point — native agent assignment — doesn't extend to Claude Code**, the agent actually doing your backend work. Adopting Linear this week would mean running your PM layer on the one tool whose flagship feature doesn't cover half your stack. Worth revisiting post-admission if the team grows and Cursor/Devin/Codex enter the picture, not now.

3. **Task Master AI is the one addition that's unambiguously worth adding now, not "later."** It's free, it turns `../ARCHITECTURE.md` / `../USERS.md` into a dependency-tracked task graph, and it exposes an MCP server — meaning a fresh Claude Code session (yours or a teammate's) can query "what's next and why" from a persistent, shared source of truth instead of you re-explaining state at the start of every session. That's a direct upgrade on the ad hoc `TaskCreate`/`TaskUpdate` tracking already happening in-session, made durable across sessions.

4. **Jira, ClickUp, Notion, Shortcut, Plane** are all reasonable tools in the abstract, but every one of them adds a context-switch and either costs more per seat or gates its agent features behind a higher tier than what you'd actually use in week one. None of them removes work you're currently doing manually — they'd be lateral moves, not upgrades, at this project's current size and timeline.

Revisit this if: the team grows past 2–3 people, the timeline extends past this admission sprint, or you specifically want Linear/Plane's richer agent-orchestration (multiple coding agents competing/collaborating on the same backlog) — none of which is the constraint you're under right now.
