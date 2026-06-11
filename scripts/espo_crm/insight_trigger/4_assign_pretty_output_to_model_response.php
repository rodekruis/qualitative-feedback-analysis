<?php // Do not copy this tag into EspoCRM
// This assigns the pretty_output of the bulk reponse to the
// modelResponse attribute

$response = $_lastHttpResponseBody;

modelResponse = json\retrieve($response, 'pretty_output');