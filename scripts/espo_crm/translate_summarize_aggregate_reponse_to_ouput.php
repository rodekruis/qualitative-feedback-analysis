<?php // Do not copy this tag into EspoCRM
// Script for translating response to output string for summarize aggregate feature in EspoCRM
// The QFA backend now returns a pre-formatted pretty_output field on the summary object.

$response = $_lastHttpResponseBody;

$modelResponse = json\retrieve($response, 'summary.pretty_output');
