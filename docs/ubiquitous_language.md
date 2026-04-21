# Ubiquitous Language

This document defines the language used in the QFA project.

The defined terms must be used consistently throughout the project, in code, documentation,
user and developer communication.

This ensures that everybody speaks the same language, and the same concept has the same
name in all contexts.

## Definitions

| Preferred term        | Avoid / replace with                 | Rationale |
|-----------------------|--------------------------------------|-----------|
| Feedback record       | Document, case, ticket, entry         | Neutral and system-agnostic; works well for qualitative, unstructured data. This feedback record contains the 'feedback description' and other metadata |
| Feedback              | Question, Complaint                 | Feedback is the umbrella term in IFRC guidance |
| Feedback description         | Message, note, transcript             | This is qualitative/unstructured data which keeps focus on content rather than channel or format |
| Feedback source       | Channel, platform (only)              | Supports both human and technical sources |
| Feedback metadata     | Extra fields, system data             | Aligns with data responsibility and analytics language |
| Feedback dataset      | Database, table                       | Analytical framing; ML- and analysis-friendly |
| Coding                | Tagging only                          | Standard qualitative analysis practice |
| Code framework        | Taxonomy, list of tags                | Emphasises flexibility and iteration |
| Auto-coding           | AI tagging                            | Tool-neutral and future-proof |
| Manual coding         | Human tagging                         | Clear distinction between automated and human steps |


## Feedback types

- Question  
  Requests for information or clarification.

- Request  
  Requests for assistance, services, or specific actions.

- Suggestion  
  Ideas or recommendations to improve services, communication, or processes.

- Concern  
  Expressions of worry, dissatisfaction, or perceived problems that may or may not require action.
- Complaint  
  Explicit negative feedback about services, behaviour, access, or quality.
- Sensitive feedback  
  Feedback that requires special handling due to protection, safeguarding, or ethical considerations.
``

## QFA language conventions

- Use **feedback** as the umbrella term for all input from community members.
- Each individual input is a **feedback record**.
- Feedback records are people-centred, not organisation-centred.
- Avoid ticket, case, or call-centre language.
- Language should be neutral, non-judgemental, and channel-agnostic.

## Analysis and processing terminology

- Feedback is **coded**, not tagged.
- Coding may be **manual** or **auto-coded**.
- Analysis focuses on **themes**, **trends**, and **insights**, not single records only.


## Avoid the following terms

- Case (legal / protection connotation)
- Ticket (call-centre framing)
- Beneficiary complaint (disempowering, outdated)
- User (for community members)
- Noise or irrelevant feedback
