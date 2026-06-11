<?php // Do not copy this tag into EspoCRM

$response = $_lastHttpResponseBody;

codingLevel1Id = json\retrieve($response, 'assigned_codes.0.coding_level_1_id');
codingLevel2Id = json\retrieve($response, 'assigned_codes.0.coding_level_2_id');
codingLevel3Id = json\retrieve($response, 'assigned_codes.0.coding_level_3_id');