from collections import Counter

from copilot.evals.cases import CASES, EvalCase
from copilot.evals.rubrics import RubricName
from copilot.fhir.client import FhirError
from copilot.fhir.fixtures import FixtureFhirClient
from copilot.rag.corpus import load_corpus

# Each rubric needs at least this many cases capable of failing it, or a green score proves nothing.
# Only the three discriminating rubrics have a floor; schema_valid and no_phi_in_logs are
# cross-cutting tripwires scored on every case (with the current fixtures nothing readable exposes
# an identifier, so no_phi cannot be independently failed — it guards against a future regression).
FALSIFIABILITY_FLOORS: dict[RubricName, int] = {
    RubricName.FACTUALLY_CONSISTENT: 15,
    RubricName.CITATION_PRESENT: 10,
    RubricName.SAFE_REFUSAL: 12,
}
EXPECTED_CI_GATE = 3


def structural_problems(cases: list[EvalCase]) -> list[str]:
    """Return structural defects in the case set (falsifiability floors, dedup, ci-gate count).

    No model calls, no fixtures — pure inspection of the case metadata.

    Args:
        cases: The golden-set cases.

    Returns:
        Human-readable problem strings; empty when the set is structurally sound.
    """
    problems: list[str] = []

    counts = Counter(case.primary_rubric for case in cases)
    for rubric, floor in FALSIFIABILITY_FLOORS.items():
        if counts[rubric] < floor:
            problems.append(
                f"falsifiability floor: {rubric.value} has {counts[rubric]} case(s) < {floor}"
            )

    # A case is redundant if it shares a primary rubric, mechanism, patient, and corpus topic with
    # another — same failure probed the same way on the same data. (Topic is part of the key so two
    # guideline cases on one patient that cite different corpora are correctly distinct.)
    seen: set[tuple[str, str, str, str | None]] = set()
    for case in cases:
        key = (case.primary_rubric.value, case.mechanism, case.patient_id,
               case.expected.corpus_topic)
        if key in seen:
            problems.append(f"{case.case_id}: redundant (rubric, mechanism, patient, topic) {key}")
        seen.add(key)

    ids = [case.case_id for case in cases]
    duplicate_ids = {case_id for case_id in ids if ids.count(case_id) > 1}
    if duplicate_ids:
        problems.append(f"duplicate case_id(s): {sorted(duplicate_ids)}")

    ci_count = sum(1 for case in cases if case.ci_gate)
    if ci_count != EXPECTED_CI_GATE:
        problems.append(f"expected {EXPECTED_CI_GATE} ci_gate case(s), found {ci_count}")

    return problems


async def _patient_terms(fhir: FixtureFhirClient, patient_id: str) -> str:
    """Return one lowercased haystack of every clinical entity string in a patient's record.

    Args:
        fhir: The fixture FHIR client.
        patient_id: The patient to read.

    Returns:
        A single lowercased string joining every problem display, medication name, and allergy
        substance — used for substring-checking that a declared-absent entity really is absent.
    """
    problems = await fhir.get_problems(patient_id)
    meds = await fhir.get_medications(patient_id)
    allergies = await fhir.get_allergies(patient_id)
    terms = (
        [p.display for p in problems]
        + [m.name for m in meds]
        + [a.substance for a in allergies]
    )
    # Some renderer fields are optional (str | None); drop the absent ones before joining.
    return " | ".join(term for term in terms if term).lower()


async def ground_truth_problems(cases: list[EvalCase]) -> list[str]:
    """Return cases whose declared ground truth contradicts the fixtures/corpus.

    Checks, per case: the patient loads; every ``absent_substances`` entity really is absent from
    the record (so the "don't fabricate/infer X" trap is genuine); and any ``corpus_topic`` has at
    least one chunk (so guideline/synthesis cases can actually be grounded). No model calls.

    Args:
        cases: The golden-set cases.

    Returns:
        Human-readable problem strings; empty when every case's ground truth holds.
    """
    fhir = FixtureFhirClient.from_seed()
    corpus_topics = {chunk.guideline for chunk in load_corpus()}
    terms_by_patient: dict[str, str] = {}
    problems: list[str] = []

    for case in cases:
        patient_id = case.patient_id
        if patient_id not in terms_by_patient:
            try:
                await fhir.get_patient(patient_id)
            except FhirError:
                problems.append(f"{case.case_id}: patient {patient_id!r} does not load")
                continue
            terms_by_patient[patient_id] = await _patient_terms(fhir, patient_id)

        haystack = terms_by_patient[patient_id]
        for substance in case.expected.absent_substances:
            if substance.lower() in haystack:
                problems.append(
                    f"{case.case_id}: absent_substance {substance!r} is actually present for "
                    f"patient {patient_id}"
                )

        topic = case.expected.corpus_topic
        if topic is not None and topic not in corpus_topics:
            problems.append(f"{case.case_id}: corpus_topic {topic!r} has no chunk in the corpus")

    return problems


async def verify_cases(cases: list[EvalCase] | None = None) -> list[str]:
    """Return every structural and ground-truth defect in the golden set (empty when honest).

    The single entrypoint the ground-truth test asserts on. Free to run — no model calls.

    Args:
        cases: Cases to verify; defaults to the full :data:`copilot.evals.cases.CASES` set.

    Returns:
        All problem strings from the structural and ground-truth checks.
    """
    cases = cases if cases is not None else CASES
    return structural_problems(cases) + await ground_truth_problems(cases)
