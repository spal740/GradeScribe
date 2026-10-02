GradeScribe
Digitises scanned clinical assessment forms and applies a marking rubric with an AI agent, flagging anything a human needs to review.

GradeScribe was built for the University of Auckland hackathon. It takes scanned or photographed assessment forms for medical students, extracts the data, checks the form is complete, applies the approved rubric and grading rules, and produces a structured result for reporting. Safety and professionalism concerns, and borderline or failing grades, are escalated to a person instead of being left to the model.

Status: hackathon prototype, built and tested on mock data. It is not a production grading system, and a human reviews every escalated result.

The problem

Clinical assessment forms are filled in by hand, scanned, then keyed in and graded manually against a rubric. That is slow, repetitive and inconsistent between markers, and concerning comments can be missed in a long stack of forms.

How it works
Azure Blob (scanned forms)
        │
        ▼
Document Intelligence (custom model)  →  extract fields + confidence
        │
        ▼
Mandatory-field validation            →  report anything missing
        │
        ▼
Payload builder                       →  per-student structured input
        │
        ▼
Rubric scoring (GPT-5 mini)           →  component grades + reasoning
        │
        ▼
Aggregation agent (Azure AI Foundry)  →  overall grade, per-domain ratings,
        │                                 fitness-to-practise flag, escalation
        ▼
Content Safety on free-text comments  →  flag concerns for human review
        │
        ▼
results.json (local + Azure Blob)
Step	What it does	Where
Extract	Runs a custom Azure Document Intelligence model on each form	src/custom_extract.py
Validate	Checks mandatory fields (rubric tick-boxes, scores, fitness to practise, student ID)	src/validation.py
Score	Applies the rubric rules with GPT-5 mini and strict structured outputs	src/scoring.py, prompts/scoring_prompt.md
Aggregate	A Foundry agent produces the overall grade and escalation decision	src/aggregation.py, prompts/aggregation_prompt.md
Screen	Azure AI Content Safety checks free-text comments	src/content_safety.py
Orchestrate	Runs the pipeline per student and writes the results	src/orchestration.py
Design decisions
A small model, because of the budget. The project had a cost limit, so scoring uses GPT-5 mini. The rubric is written as explicit rules, so the model applies thresholds instead of making open-ended judgements.
Structured outputs, not free text. Scoring and aggregation use strict JSON schemas, so grades are limited to a fixed set (Distinction, Pass, Borderline, Fail) at the API level, not only by the prompt.
Human in the loop. Escalation is triggered by a grade threshold or a fitness-to-practise concern, and every escalated result is marked review_required.
Safety checks sit outside the agent. Free-text comments go to Content Safety as a separate branch. The grading agent never sees them.
No model where none is needed. Supervisor comments and form data are passed through in plain Python, with no LLM and no rewriting.
Run it

Requires Python 3.11 and an Azure subscription with Blob Storage, Document Intelligence, Azure OpenAI, an AI Foundry project and Content Safety.

bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then fill in your own values
python -m src.orchestration

Configuration is read from environment variables (never commit your .env): Azure OpenAI endpoint, key, API version and deployment; Document Intelligence endpoint, key and custom model ID; Content Safety endpoint and key; Foundry project endpoint; and the storage account and container names.

Data and privacy

The repository contains mock data only. Do not commit real student, assessor or patient information, and keep keys out of version control.

Author

Built by Suhasini Palanimuthu.
