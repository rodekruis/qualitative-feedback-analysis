## Problem

`SummarizeService.summarize_bulk` (`src/qfa/services/summarize.py:201-207`) builds one envelope from every record in `request.feedback_records`. It then calls `self._anonymizer.anonymize(user_message)` on that envelope, directly on the FastAPI event loop. The Presidio-based anonymizer runs a CPU-bound NER pass. Its duration grows with the size of the corpus.

`AnalyzeService.analyze_bulk` had the same problem. Issues #325 and #327 fixed it. Anonymisation now runs off the event loop through `asyncio.to_thread` (`src/qfa/services/analyze.py:281-290`). The synchronous call had blocked the event loop long enough to stall the request's own heartbeat coroutine. In production, it also stalled the gunicorn worker itself. `summarize_bulk` still calls the anonymizer directly. It was not part of that fix.

`ApiBulkInferenceRequestBase.feedback_records` (`src/qfa/api/schemas.py:419-422`) only enforces `min_length=1`. No upper bound exists on how many records a request can submit.

## Trigger

If a request to `/v1/summarize-bulk` submits a large number of feedback records, anonymisation of the full concatenated batch runs on the event loop. This can block the event loop for the same reason #325 blocked `analyze_bulk`.

## Suggested fix

Apply the same `asyncio.to_thread` offload used in `analyze_bulk` to the anonymisation call in `summarize_bulk`.

## Related

- #325 (the identical bug, already fixed for `analyze_bulk`)
- #328 (a separate, open feature request to speed up anonymisation. This issue is about a correctness gap, not raw speed)
