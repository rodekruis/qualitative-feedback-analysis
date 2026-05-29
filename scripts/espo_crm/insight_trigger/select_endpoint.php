<?php // Do not copy this tag into EspoCRM
// This selects the endpoint to hit based on the method chosen in the UI

$selected_method = record\attribute('CInsight', id, 'method');

if ($selected_method == "code") {$$endpoint = "/v1/assign_codes";} 
else if ($selected_method == "summarize_aggregate") {$$endpoint = "/v1/summarize-aggregate";}
else if ($selected_method == "summarize_per_item") {$$endpoint = "/v1/summarize";}
else if ($selected_method == "analyze") {$$endpoint = "/v1/analyze";}
else {$$endpoint = "/v1/summarize";}

$fullEndpoint = string\concatenate("https://obsessed-mantra-visible.ngrok-free.dev", $$endpoint);
