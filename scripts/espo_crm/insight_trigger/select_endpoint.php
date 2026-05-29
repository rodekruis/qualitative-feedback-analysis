<?php // Do not copy this tag into EspoCRM
// This selects the endpoint to hit based on the method chosen in the UI

$$selectedMethod = record\attribute('CInsight', id, 'method');

if ($$selectedMethod == "code") {$$endpoint = "/v1/assign-codes";} 
else if ($$selectedMethod == "summarize_aggregate") {$$endpoint = "/v1/summarize-aggregate";}
else if ($$selectedMethod == "summarize_per_item") {$$endpoint = "/v1/summarize";}
else if ($$selectedMethod == "analyze") {$$endpoint = "/v1/analyze";}
else {$$endpoint = "/v1/summarize";}

$fullEndpoint = string\concatenate("https://obsessed-mantra-visible.ngrok-free.dev", $$endpoint);
