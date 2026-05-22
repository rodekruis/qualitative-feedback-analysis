<?php // Do not copy this tag into EspoCRM
// This creates a "mother" payload that can be used to hit any of the inference routes

$motherPayload = string\concatenate(
    '{',
    '"feedback_records": ', $$recordsString, ', ',
    '"coding_framework": ', $$codesString, '',
    '}'
);