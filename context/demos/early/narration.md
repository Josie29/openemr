# Early Submission Demo — Clinical Co-Pilot: a trustworthy agent, not a chatbot

## Intro

A physician has ninety seconds between rooms to recall who they're seeing and what
actually matters today. The Clinical Co-Pilot is an AI agent embedded directly in
OpenEMR that answers that question from *this* patient's real record. The hard part
isn't the answer — it's trust: a confidently stated hallucination in a clinical
setting can harm a patient. So every decision here optimizes for a claim you can
verify, not a demo that looks smart.

On screen: open Sergio Angulo's chart (pid 23 — rich record: 8 meds, 9 allergies, 98 encounters); hover the "Co-Pilot" launcher in the patient banner.

## Beat 1 — The agent, and why every answer cites the record

We built a conversational agent, not a search box, because a physician's real questions
are follow-ups — "what changed," "any conflicts," "what's overdue." Watch the empty
state: it promises up front that every answer cites the record it came from. I'll ask it
to summarize the patient.

On screen: click "Co-Pilot" to open the sidebar; read the intro line "Every answer cites the record it came from"; click into the composer and send "Summarize this patient".

The answer isn't one paragraph of prose — it's a summary followed by discrete claims,
and each claim carries a citation to a specific FHIR resource and field. That structure
is the product: the physician can trace any statement back to the chart in one glance.

On screen: after the answer renders, point to the ordered claims and expand a citation showing resource_type / id and the field value.

## Beat 2 — Grounding is enforced at runtime, not just measured

Here's the decision the grader is listening for. Most systems check the model *after* it
answers. We made grounding a hard gate *inside* the agent: an output validator resolves
every claim against the exact records the tools actually read this turn. If a claim can't
be resolved, the model is forced to re-ground or drop it. And the value you see in each
citation is stamped in by code from the real record — the model never writes it. So the
number on screen is the number in the chart, by construction.

On screen: (narration over the same cited answer) hover a citation value to emphasize it matches the underlying field.

## Beat 3 — Who's asking: the authorization boundary

The case study's first hard problem is access control. Our answer is structural: the
panel never trusts a patient ID from the page. It runs a SMART launch to mint a
patient-scoped token, and the agent builds a fresh, token-scoped data client per request.
It physically cannot read a patient the clinician doesn't have open — there are no
database credentials in the agent at all. Authorization isn't a runtime check bolted on;
it's the only door in.

On screen: switch from Sergio to Rex Upton's chart (pid 19); show the Co-Pilot header re-scope to Rex (title and patient name follow the open chart).

## Beat 4 — Failure and missing data, on purpose

A clinical tool that silently fails is worse than none. So when a record is sparse, the
agent says so plainly — "no drug allergies are recorded" — instead of inventing
reassurance. When it genuinely can't attribute an answer, it refuses rather than guesses.
And when the data source is unreachable, it reports the gap. Graceful degradation is a
feature we designed for, not an accident we hope to avoid.

On screen: on Rex Upton's chart (near-empty record), ask "Any medication or allergy conflicts?" and show the plain, honest "no medications or allergies are recorded" answer.

## Beat 5 — Evals and observability: how we know it works

Because this is a trust system, we measure trust. The eval suite runs the real model
against fixture patients and scores four things: did it call the right tools, did it
avoid fabrication, is the summary faithful to the verified claims, and is it complete —
including cases that exist only to prove it handles missing data. Every request is traced
in Langfuse with the grounding gate's own pass/fail score, so we can reconstruct exactly
what the agent did, how long it took, and what it cost, from logs alone.

On screen: switch to the Langfuse dashboard; show a trace with tool calls and the verification_grounding score, then the eval run with the four scorers.

## Close

That's the Early Submission: a deployed agent whose every claim is grounded in the
record, scoped to the right patient, honest about what it doesn't know, and measured end
to end. Next is hardening the verification judges and the observability dashboards toward
the final.

On screen: return to the OpenEMR chart with the Co-Pilot open on a cited answer, and leave it there.
