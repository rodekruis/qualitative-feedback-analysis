<?php // Do not copy this tag into EspoCRM
// Script for settings payload containing all codes in EspoCRM
// Works with the CodingLevels model

$ids1 = record\findMany('CCodingLevel1', 20, 'name', 'asc');
$nodes1 = list();
$i = 0;

while ($i < array\length($ids1)) {
    $id1 = array\at($ids1, $i);

    $name1 = record\attribute('CCodingLevel1', $id1, 'name');
    ifThen($name1 == null || string\length($name1) == 0, $name1 = $id1);

    // Search for Level 2s that point to this Level 1
    $ids2 = record\findMany('CCodingLevel2', 50, 'name', 'asc', 'codingLevel1Id=', $id1);
    $nodes2 = list();
    $j = 0;

    while ($j < array\length($ids2)) {
        $id2 = array\at($ids2, $j);

        $name2 = record\attribute('CCodingLevel2', $id2, 'name');
        ifThen($name2 == null || string\length($name2) == 0, $name2 = $id2);

        // Search for Level 3s that point to this Level 2
        $ids3 = record\findMany('CCodingLevel3', 50, 'name', 'asc', 'codingLevel2Id=', $id2);
        $nodes3 = list();
        $k = 0;

        while ($k < array\length($ids3)) {
            $id3 = array\at($ids3, $k);

            $name3 = record\attribute('CCodingLevel3', $id3, 'name');
            ifThen($name3 == null || string\length($name3) == 0, $name3 = $id3);

            $node3 = object\create();
            $node3['id'] = $id3;
            $node3['name'] = $name3;
            $node3['children'] = list();

            $nodes3 = array\push($nodes3, json\encode($node3));
            $k = $k + 1;
        }

        $node2 = object\create();
        $node2['id'] = $id2;
        $node2['name'] = $name2;
        $node2['children'] = json\retrieve(string\concatenate('[', array\join($nodes3, ','), ']'));

        $nodes2 = array\push($nodes2, json\encode($node2));
        $j = $j + 1;
    }

    $node1 = object\create();
    $node1['id'] = $id1;
    $node1['name'] = $name1;
    $node1['children'] = json\retrieve(string\concatenate('[', array\join($nodes2, ','), ']'));

    $nodes1 = array\push($nodes1, json\encode($node1));
    $i = $i + 1;
}

$$codesString = string\concatenate('[', array\join($nodes1, ','), ']');