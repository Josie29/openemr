"""Grounding-and-faithfulness eval harness for the Clinical Co-Pilot agent.

Cases run the real agent against the bundled FHIR fixtures (no live OpenEMR, no PHI) and score
each turn with two deterministic checks (tool-correctness, no-fabrication) and two Haiku
LLM-as-judge rubrics (summary-faithfulness, answer-completeness), reporting to Langfuse. See
``README.md`` in this package for how to seed the dataset and run an experiment.
"""
