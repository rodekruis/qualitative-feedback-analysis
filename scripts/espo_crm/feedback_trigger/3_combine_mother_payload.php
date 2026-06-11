<?php // Do not copy this tag into EspoCRM

// Detect sensitive
// Codes
// Summarize one item

$motherPayload = string\concatenate(
    '{',
    '"feedback_record": ', $$recordString, ', ',
    '"coding_levels": {"root_codes": ', $$codesString, '}, ',
    '"max_codes": 1, ',
    '"confidence_threshold": 0.1 ',
    '}'
);