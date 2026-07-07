---
week: 1
project: AgentForge — Clinical Co-Pilot
track: Gauntlet AI — Austin Admission Track
deadline: 2026-07-12 (Sunday @ Noon CT)
stack: TBD
status: in-progress
---

# AgentForge | Clinical Co-Pilot
**Building Production-Ready AI Agents for Healthcare**

Project Requirements Document — Gauntlet AI, Austin Admission Track

---

## How to Use This Case Study

This case study is the north star for the duration of this project. It must be used as the foundation for every decision made: what to build, what to prioritize, and what to expand. It does not define the ceiling — going deeper or broader is encouraged, but in the spirit of the case study, not in spite of it.

What it does define is the floor: every feature, architectural decision, and tradeoff must be traceable back to the problem of a customer needing reliable, fast, secure access to data. Use this document as a reference, a constraint, and a lens.

The decisions made this week are the foundation for weeks two and three — good architecture compounds; technical debt costs double later. Evaluation criteria: thoroughness, thoughtfulness, creativity, and ability to leverage technology to build something viable.

## The Scenario

A physician has 90 seconds between patient rooms. In that window, they need to recall who they're seeing, why, what's changed since the last visit, what's on file, and what actually matters today. Right now that means scanning dense EHR notes, flipping through lab results, cross-referencing medication lists — all under pressure while the patient waits.

**The task:** build a Clinical Co-Pilot — an AI agent embedded directly into OpenEMR that gives a physician the context they need, the moment they need it. Not a chatbot that answers generic medical questions. A system that knows this patient, their history, their meds, their recent labs, and can surface what's relevant to today's visit in a conversation-style interface.

> **Why this matters:** A confidently stated hallucination in a clinical setting doesn't just damage trust — it can directly harm a patient. The gap between a prototype that works in a demo and an agent that can be trusted in a hospital is the entire scope of this project.

## The Hard Problems

This is not a build-whatever-you-want project. The surface area is intentionally constrained, but the engineering problems inside it are real and unsolved.

### Authorization & Access Control
Who is allowed to query patient data? A physician has access to their own patients. A nurse may have different permissions. A resident may be supervised. The system must know who is asking and enforce appropriate access — not assume all users are trusted. Multi-user environments are the norm clinically, and the architecture must reflect that.

### Verification & Trust
Every claim the agent makes must be traceable back to a source in the patient's actual record — not inferred, not assumed, not hallucinated. The agent must also respect domain constraints: clinical rules, dosage thresholds, interaction flags. A response that violates what the underlying data says is a failure, not a feature. Implementation approach is open, but it must be deliberate and defensible.

### Speed vs. Completeness
A physician needs an answer in seconds, not minutes — but a complete answer might require pulling multiple data sources, running multiple tool calls, and synthesizing conflicting records. What to prioritize, what to defer, and how to communicate uncertainty is a core, explicit design decision.

### Data Security & HIPAA
Patient health information is PHI under HIPAA — a legal and ethical constraint shaping every architectural decision: storage, transmission, logging, access. The audit and architecture docs must demonstrate real understanding of these constraints, not just the acronym.

> **Note:** Only use demo data with this codebase. For all Gauntlet projects, act as if a signed Business Associate Agreement (BAA) exists with all LLM providers guaranteeing no data will be used for training.

### Failure Modes
What happens when a tool fails? When a patient record is incomplete? When the model returns something unexpected? A clinical tool that crashes or silently fails is worse than no tool at all. Graceful degradation, transparent errors, and predictable behavior under failure are not nice-to-haves.

## The Codebase: OpenEMR

Fork OpenEMR — a widely-deployed, open-source EHR system with a large, real codebase. This is the foundation; the task is integrating an AI agent into existing healthcare infrastructure, not building a clinical app from scratch.

**Fork from:** https://github.com/Gauntlet-HQ/openemr-base-clean

The codebase will likely be unfamiliar. Part of the project is demonstrating the ability to orient in a large, complex system, understand its architecture, identify where new work fits, and integrate cleanly rather than bolt something on.

> **Gate:** Project completion and interviews are both required for Austin admission. The audit is a hard gate — it must be completed before building the AI layer.

## Project Schedule

One-week sprint, four checkpoints. All times Central (Austin).

| Checkpoint | Deadline | Focus |
|---|---|---|
| Architecture Defense | 24 hours | Architecture research and planning |
| MVP | Tuesday @ 11:59 PM | App audit, agent plan, deployed app, demo video. AI Interview required 24 hrs after submission. |
| Early Submission | Thursday @ 11:59 PM | Deployed agent, eval framework in place, observability wired in, demo video. AI Interview required 24 hrs after submission. |
| Final | Sunday @ Noon | Production-ready agent, demo video, social media post. AI Interview required 24 hrs after submission. |

## MVP: Recommended Steps

The MVP is not a working agent — it's the foundation that makes a trustworthy agent possible.

| Stage | Name | Deliverable |
|---|---|---|
| 1 | Run It Locally | OpenEMR running locally with sample patient data |
| 2 | Deploy It | Publicly accessible deployment of the OpenEMR fork |
| 3 | Audit It | Full audit with written record of findings |
| 4 | Identify Users | Breakdown of target users and their use cases |
| 5 | Plan the Agent | Concrete, codebase-informed plan for the Clinical Co-Pilot |

### Stage 1 — Run It Locally
Get OpenEMR running locally with realistic sample patient data. You cannot audit or build what you cannot run. Document the setup process — this becomes part of the README and demonstrates understanding of the system's dependencies.

### Stage 2 — Deploy It
Deploy the fork to a publicly accessible environment. Doesn't need to be production-hardened yet, but must be live and reachable. The final agent deploys to the same infrastructure, so choose the stack thoughtfully.

> **Hard Gate:** The deployed app's URL must be submitted with every submission.

### Stage 3 — Audit It
Before any additions, complete a full audit of the system, covering at minimum:

- **Security audit** — authentication/authorization risks, data exposure vectors, PHI handling issues, HIPAA-relevant gaps
- **Performance audit** — bottlenecks, data structure, constraints affecting agent response latency
- **Architecture audit** — system organization, where data lives, layer interaction, integration points for new capabilities
- **Data Quality audit** — completeness, consistency, reliability of the data; missing fields, inconsistent formatting, duplicate records, stale data as agent failure modes
- **Compliance & Regulatory audit** — audit logging requirements, data retention policies, breach notification obligations, BAA implications of sending PHI to an LLM provider

> **Hard Gate:** `./AUDIT.md` — all audit findings, beginning with a ~500-word one-page summary of key findings. The brevity requirement is intentional: highlight the most impactful findings, not a dump of everything.

### Stage 4 — Create User Profiles and Use Cases
Before planning an agent, decide who it's actually for and what specific problem it solves for them. "Physicians need help finding information" is not a user definition — it's a thesis statement that has produced a thousand failed health-tech products.

Pick a real, narrow user: a primary care physician with a 20-patient day, an ED resident on overnight intake, a hospitalist rounding on twelve admissions before noon. These are different people with different workflows, pain points, and tolerances for agent behavior. The chosen user constrains everything downstream: what data the agent needs, how fast it must respond, what it should refuse to do, what "useful" even means.

Ground that user in a concrete workflow: what are they doing thirty seconds before opening the agent, what do they need from it, what do they do with the output? Identify specific use cases — not "answer questions about a patient" but "between 8:50 and 9:00 AM, surface what's changed for each patient on today's schedule and flag anything that needs attention." For each, be ready to defend why a conversational agent is the right shape — not a dashboard, not a sorted list, not a better chart view.

The bar is not that an agent is technically possible. The bar is that the agent is the thing the user would actually choose.

> **Hard Gate:** `./USERS.md` — target user, their workflow, and specific use cases the agent addresses. Each use case must explicitly answer why an agent is the right solution. This document is the source of truth `ARCHITECTURE.md` must trace back to — every agent capability built in Stage 5 must point to a use case here.

### Stage 5 — Develop the AI Integration Plan
Using `AUDIT.md` findings as input, synthesize a forward-looking roadmap: where the agent lives, how it accesses patient data, authorization boundaries, risks, and mitigations. No implementation required yet — just clear thinking, written down, defensible (defended Tuesday). This becomes the roadmap for Early Submission.

> **Hard Gate:** `./ARCHITECTURE.md` — how the agent will be built to address the case study, beginning with a ~500-word one-page summary of high-level architecture, key decisions, major considerations, and tradeoffs.

## Agent Requirements

### Agentic Chatbot
The core interface is a conversational agent — a multi-turn AI agent that receives follow-up questions, maintains context across a conversation, and invokes tools to retrieve and reason over patient data. Not a search bar, dashboard widget, or report generator.

Every agent capability must trace to a specific user problem identified in `USERS.md`. No use case requiring multi-turn conversation → no multi-turn conversation. No use case requiring tool chaining → no tool chaining. The agent's surface area is determined by user needs, not by what's technically interesting to build.

### Verification System
Every response must pass through a verification layer before reaching the user, ensuring claims are actually supported by the patient's data:

- **Source attribution** — claims must be traceable to specific records in the patient's file; unattributable claims should not be stated as fact
- **Domain constraint enforcement** — the agent must be aware of clinical rules and flag or reject responses that violate them (design and enforcement approach is open)

Document where in the agent's flow verification happens, what it catches, and its known limitations.

### Observability
Implement observability from the start, not as an afterthought. At minimum, be able to answer from logs at any time:

- What did the agent do on a specific request, and in what order?
- How long did each step take?
- Did any tools fail, and why?
- How many tokens were consumed, and at what cost?

Tool choice, additional metrics, and visualization approach are open — the requirement is that observability is real, wired in from the beginning, and actually used.

### Evaluation
Build a test suite to measure whether the agent is working. Test scope, case count, and pass/fail definitions are design decisions, but must be intentional and defensible.

A strong eval suite surfaces failure modes, regression risks, and clinically relevant edge cases: missing data, ambiguous queries, inputs attempting to extract information the requester isn't authorized to see — not just happy paths.

## Engineering Requirements

Applies in addition to the project-specific deliverables above; graded alongside the core submission, not optional.

- **Test design for boundaries, invariants, and regression** — every eval case must exercise a boundary condition (missing data, malformed input, empty patient record), an invariant (claims always cite a source), or a known regression risk. Happy-path-only suites do not pass. Document the failure mode each test guards against.
- **Correlation IDs across service boundaries** — every agent invocation gets a unique correlation ID appearing in every log entry, tool call, and LLM interaction, so a full trace can be reconstructed from logs alone.
- **Canonical API/event/schema contracts** — strict schemas (Pydantic, Zod, or equivalent) for every tool input/output; contracts are the source of truth, not the implementation.
- **Dashboards** — real-time: total requests, error rate, p50/p95 latency, tool call counts, retry counts, verification pass/fail rate (LangSmith, Langfuse, Braintrust, or equivalent). Add metrics relevant to the specific agent design.
- **Runnable API collection** — export a Postman/Bruno/equivalent collection covering core agent endpoints; graders must be able to run any workflow from it without reading source code.
- **Separate `/health` and `/ready` endpoints** — `/health` = process alive; `/ready` must actually validate that OpenEMR, the LLM provider, and the observability backend are reachable, not just return 200 unconditionally.
- **Alert definitions** — at least three alerts: p95 latency exceeding threshold, error rate exceeding threshold, tool failure rate. Document what each means and the on-call response.
- **Baseline infrastructure profiles** — CPU, memory, request latency, throughput under load test scenarios, included in the submission as a future comparison baseline.
- **Load/stress tests** — simulate at least 10 and 50 concurrent users against the deployed agent; record p50/p95/p99 latency and error rate at each level.

## Submission Requirements

**Final deadline: Sunday 11:59 AM CT** *(note: this conflicts with the "Sunday @ Noon" stated in the schedule table above — worth clarifying with staff which is authoritative)*

| Deliverable | Requirements |
|---|---|
| GitHub Repository | Forked from OpenEMR. Includes setup guide, architecture overview, deployed link. |
| Audit Document (`./AUDIT.md`) | All audit findings with a 1-page (~500 word) summary of key findings. |
| User Doc (`./USER.md`) | Target user with a list of use cases the agent addresses. |
| Agent Architecture Doc (`./ARCHITECTURE.md`) | Integration plan with technical details (framework choices, verification strategy, tradeoffs). Must begin with a 1-page (~500 word) summary. |
| Demo Video (3–5 min) | Per submission, showcasing work, key decisions, and the product. |
| Eval Dataset | Test suite with results; structure and scope are design decisions. |
| AI Cost Analysis | Actual dev spend and projected production costs at 100 / 1K / 10K / 100K users, plus architectural changes needed at each level. Not simply cost-per-token × n users. |
| Deployed Application | Publicly accessible; must work live for early and final submissions. |
| Social Post (final submission only) | Share on X or LinkedIn describing the project, showing the agent, tagging @GauntletAI. |

## Interview Preparation

Austin admission requires an interview with each major deliverable. Be ready to discuss work in depth.

**Your Audit**
- Walk through the most important finding.
- What would've been missed skipping straight to building?
- How did the audit change the AI integration plan?

**Your Architecture**
- Why was the verification layer designed this way?
- What does the agent do when a tool fails or a record is missing?
- Where are the trust boundaries, and how are they enforced?

**Your Evaluation**
- What does the eval suite test that a happy-path demo wouldn't reveal?
- What did running it reveal?
- What would be added next?

**Production Thinking**
- How would this scale to a 500-bed hospital with 300 concurrent clinical users?
- What would need to change before a real physician could rely on this?
- What failure mode is most concerning, and why?

> **Final Note:** The deliverable that matters is not the one that looks most impressive in a demo. It's the one that could be defended in front of a hospital CTO deciding whether to put it in front of their physicians. That is the standard. Build to it.

---

## Appendix: Pre-Search Checklist

Use this list to ensure a variety of perspectives have been considered in planning.

### Phase 1: Define Your Constraints

**1. Domain Selection**
- What specific use cases will be supported?
- What are the verification requirements for this domain?
- What data sources are needed?

**2. Scale & Performance**
- Expected query volume?
- Acceptable latency for responses?
- Concurrent user requirements?
- Cost constraints for LLM calls?

**3. Reliability Requirements**
- Cost of a wrong answer in this domain?
- What verification is non-negotiable?
- Human-in-the-loop requirements?
- Audit/compliance needs?

**4. Team & Skill Constraints**
- Familiarity with agent frameworks?
- Experience with the domain?
- Comfort with eval/testing frameworks?

### Phase 2: Architecture Discovery

**5. Agent Framework Selection**
- Single agent or multi-agent architecture?
- State management requirements?
- Tool integration complexity?

**6. LLM Selection**
- OpenAI vs. Claude vs. open source?
- Structured output support requirements?
- Context window needs?
- Cost per query acceptable?

**7. Tool Design**
- What tools does the agent need?
- External API dependencies?
- Mock vs. real data for development?
- Error handling per tool?

**8. Observability Strategy**
- LangSmith vs. Langfuse vs. Braintrust vs. other?
- What metrics matter most?
- Real-time monitoring needs?
- Cost tracking requirements?

**9. Eval Approach**
- How will correctness be measured?
- Ground truth data sources?
- Automated vs. human evaluation?
- CI integration for eval runs?

**10. Verification Design**
- What claims must be verified?
- Fact-checking data sources?
- Confidence thresholds?
- Escalation triggers?

### Phase 3: Post-Stack Refinement

**11. Failure Mode Analysis**
- What happens when tools fail?
- How to handle ambiguous queries?
- Rate limiting and fallback strategies?
- Graceful degradation approach?

**12. Security Considerations**
- Prompt injection prevention?
- Data leakage risks?
- API key management?
- Audit logging requirements?

**13. Testing Strategy**
- Unit tests for tools?
- Integration tests for agent flows?
- Adversarial testing approach?
- Regression testing setup?

**14. Open Source Planning**
- What will be released?
- Licensing considerations?
- Documentation requirements?
- Community engagement plan?

**15. Deployment & Operations**
- Hosting approach?
- CI/CD for agent updates?
- Monitoring and alerting?
- Rollback strategy?

**16. Iteration Planning**
- How will user feedback be collected?
- Eval-driven improvement cycle?
- Feature prioritization approach?
- Long-term maintenance plan?

---

## Working notes / scope decisions
*(keep annotations here, separate from the verbatim requirements above)*

-