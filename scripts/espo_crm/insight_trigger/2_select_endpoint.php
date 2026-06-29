<?php // Do not copy this tag into EspoCRM
// This selects the endpoint to hit based on the method chosen in the UI

$$selectedMethod = record\attribute('CInsight', id, 'method');
$url= ext\appSecret\get('QFA_API_BASE_URL');

if ($$selectedMethod == "summarize_aggregate") {$$endpoint = "/v1/summarize-bulk";}
else if ($$selectedMethod == "analyze") {$$endpoint = "/v1/analyze-bulk";}
else {$$endpoint = "/v1/summarize-bulk";}

// Currently hard-coded until upgraded to espo 9.2.3+
$fullEndpoint = string\concatenate($url, $$endpoint);
