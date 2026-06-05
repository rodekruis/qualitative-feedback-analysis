<?php // Do not copy this tag into EspoCRM

// Detect sensitive
// Codes
// Summarize one item

$motherPayload = string\concatenate(
    '{',
    '"feedback_record": ', $$recordString, ', ',
    '"coding_framework": {"root_codes": ', $$codesString, '}, ',
    '"max_codes": 10, ',
    '"confidence_threshold": 0.7, ',
    '}'
);