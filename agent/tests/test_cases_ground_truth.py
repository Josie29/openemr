from copilot.evals.cases import CASES
from copilot.evals.verify_cases import verify_cases


async def test_golden_set_ground_truth_is_honest() -> None:
    # Catches a golden-set case whose expectation contradicts the fixtures/corpus (an "absence" trap
    # on an entity the patient actually has, a corpus_topic with no chunks, a wrong patient_id), a
    # falsifiability-floor breach (a rubric with too few cases that could fail it — a green score
    # that proves nothing), a redundant case, or a drifted ci_gate count. Without this guard the
    # eval can silently rot into testing nothing as the fixtures evolve. Runs free — no model calls.
    problems = await verify_cases()
    assert problems == [], "golden set defects:\n" + "\n".join(problems)


def test_golden_set_is_fifty_cases() -> None:
    # The suite is sized at 50 by design (the coverage matrix). A large accidental change to the
    # count means the matrix allocation drifted and coverage should be re-reviewed.
    assert len(CASES) == 50
