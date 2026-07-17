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


def test_golden_set_size() -> None:
    # The suite is sized by the coverage matrix: the PRD's 50, plus the both-tools
    # extract+guideline synthesis case, plus angulo-hemoglobin-series (the lab-read answer case
    # that lands with JOS-82). A change to the count means the matrix allocation drifted and
    # coverage should be re-reviewed — swapping a case in and out keeps this green, which is
    # intended; the ground-truth and falsifiability checks above are what police the swap.
    assert len(CASES) == 53
