## Scope

#391 now builds the English pool. It contains themes that match the 10 prompts,
urgent records and vulnerable groups. That was most of this ticket.

What stays here is Spanish. The Red Cross prompts exist in English and Spanish,
but the pool is English only.

## Tasks

1. Translate the pool into `feedback/records-es-v1`. Each record gets a new id, the
   same labels, and `base_id` with the id of its English record.
2. Add Spanish keywords to the vocabulary entries. Langfuse keeps the old version.
   Run the vocabulary checks again.
3. Ask a native speaker to review a sample. Start with the urgent and group
   records.
4. Run the masking script again on the Spanish records and prompts. The Spanish
   language model of the anonymizer has no NRP tag, so the results differ from
   English.
5. Build `analyze/cases-frequent-es-v1`: the Spanish prompts with the Spanish
   records, and `output_language` set to Spanish.
6. Add a `language_match` score to the harness of #392: `langdetect`, with a
   fixed seed, finds the case language.
7. Compare `judge_failed` between the languages first. The judge parser expects
   English labels. A Spanish judge failure is a product bug, not a quality gap.

## Acceptance criteria

- [ ] Each Spanish record has the labels of its English record, and its
      `base_id` is the id of exactly one English record.
- [ ] When you match the records by `base_id`, the answer key is the same for
      both languages. Only the keywords are different.
- [ ] The Spanish case set ran 3 times. `judge_failed` is compared between the
      languages.

## Not in this ticket

- Sending codes in the record metadata, as production does
- Other languages
- A test against real, anonymized feedback, when it is available

