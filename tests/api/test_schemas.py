"""Tests for API schemas."""

import pytest
from pydantic import ValidationError

from qfa.api.schemas import ApiAssignedCode, ApiCodingFramework, ApiCodingNode


def test_max_child_depth_leaf_returns_zero():
    assert ApiCodingNode(id="a", name="a").max_child_depth() == 0


def test_max_child_depth_single_level():
    node = ApiCodingNode(
        id="root", name="root", children=[ApiCodingNode(id="child", name="child")]
    )
    assert node.max_child_depth() == 1


def test_max_child_depth_two_levels():
    node = ApiCodingNode(
        id="root",
        name="root",
        children=[
            ApiCodingNode(
                id="mid", name="mid", children=[ApiCodingNode(id="leaf", name="leaf")]
            )
        ],
    )
    assert node.max_child_depth() == 2


def test_max_child_depth_returns_deepest_branch():
    node = ApiCodingNode(
        id="root",
        name="root",
        children=[
            ApiCodingNode(id="shallow", name="shallow"),
            ApiCodingNode(
                id="deep",
                name="deep",
                children=[ApiCodingNode(id="deeper", name="deeper")],
            ),
        ],
    )
    assert node.max_child_depth() == 2


def test_min_child_depth_leaf_returns_zero():
    assert ApiCodingNode(id="a", name="a").min_child_depth() == 0


def test_min_child_depth_single_level():
    node = ApiCodingNode(
        id="root", name="root", children=[ApiCodingNode(id="child", name="child")]
    )
    assert node.min_child_depth() == 1


def test_min_child_depth_two_levels():
    node = ApiCodingNode(
        id="root",
        name="root",
        children=[
            ApiCodingNode(
                id="mid", name="mid", children=[ApiCodingNode(id="leaf", name="leaf")]
            )
        ],
    )
    assert node.min_child_depth() == 2


def test_min_child_depth_returns_shallowest_branch():
    node = ApiCodingNode(
        id="root",
        name="root",
        children=[
            ApiCodingNode(id="shallow", name="shallow"),
            ApiCodingNode(
                id="deep",
                name="deep",
                children=[ApiCodingNode(id="deeper", name="deeper")],
            ),
        ],
    )
    assert node.min_child_depth() == 1


def test_coding_levels_valid_3_levels_enforced():
    """Flat tree (depth 0) must now have 3 levels. Test the new minimum requirement."""
    with pytest.raises(ValueError, match="exactly 3 levels"):
        ApiCodingFramework(
            root_codes=[
                ApiCodingNode(id="a", name="a"),
                ApiCodingNode(id="b", name="b"),
            ]
        )


def test_coding_levels_valid_uniform_depth():
    levels = ApiCodingFramework(
        root_codes=[
            ApiCodingNode(
                id="water-1",
                name="Water",
                children=[
                    ApiCodingNode(
                        id="dist-1",
                        name="Distribution",
                        children=[ApiCodingNode(id="wait-1", name="Waiting times")],
                    )
                ],
            ),
            ApiCodingNode(
                id="health-1",
                name="Health",
                children=[
                    ApiCodingNode(
                        id="staff-1",
                        name="Staff",
                        children=[ApiCodingNode(id="supplies-1", name="Supplies")],
                    )
                ],
            ),
        ]
    )
    assert len(levels.root_codes) == 2


def test_coding_levels_unequal_depth_raises():
    with pytest.raises(ValueError, match="same depth"):
        ApiCodingFramework(
            root_codes=[
                ApiCodingNode(id="flat", name="flat"),
                ApiCodingNode(
                    id="deep",
                    name="deep",
                    children=[ApiCodingNode(id="child", name="child")],
                ),
            ]
        )


def test_coding_levels_unequal_depth_within_subtree_raises():
    with pytest.raises(ValueError, match="same depth"):
        ApiCodingFramework(
            root_codes=[
                ApiCodingNode(
                    id="root",
                    name="root",
                    children=[
                        ApiCodingNode(id="shallow", name="shallow"),
                        ApiCodingNode(
                            id="deep",
                            name="deep",
                            children=[ApiCodingNode(id="deeper", name="deeper")],
                        ),
                    ],
                ),
            ]
        )


def test_coding_levels_with_no_children_fail():
    with pytest.raises(ValidationError, match="should have at least 1"):
        ApiCodingFramework(root_codes=[])


def test_coding_node_missing_id_raises():
    """ApiCodingNode requires id field."""
    with pytest.raises(ValidationError, match="id"):
        ApiCodingNode(name="test")  # type: ignore


def test_coding_levels_depth_less_than_3_raises():
    """Coding framework must have exactly 3 levels (depth=2). Depth < 2 should raise."""
    with pytest.raises(ValueError, match="exactly 3 levels"):
        ApiCodingFramework(
            root_codes=[
                ApiCodingNode(id="type-1", name="Type A"),
                ApiCodingNode(id="type-2", name="Type B"),
            ]
        )


def test_coding_levels_depth_more_than_3_raises():
    """Coding framework must have exactly 3 levels (depth=2). Depth > 2 should raise."""
    with pytest.raises(ValueError, match="exactly 3 levels"):
        ApiCodingFramework(
            root_codes=[
                ApiCodingNode(
                    id="type-1",
                    name="Type A",
                    children=[
                        ApiCodingNode(
                            id="cat-1",
                            name="Category A",
                            children=[
                                ApiCodingNode(
                                    id="code-1",
                                    name="Code A",
                                    children=[
                                        ApiCodingNode(
                                            id="extra-1",
                                            name="Extra Level",
                                        )
                                    ],
                                )
                            ],
                        )
                    ],
                )
            ]
        )


def test_coding_levels_valid_3_levels_with_ids():
    """Valid 3-level framework with all required ids."""
    levels = ApiCodingFramework(
        root_codes=[
            ApiCodingNode(
                id="water-1",
                name="Water",
                children=[
                    ApiCodingNode(
                        id="dist-1",
                        name="Distribution",
                        children=[ApiCodingNode(id="wait-1", name="Waiting times")],
                    )
                ],
            ),
            ApiCodingNode(
                id="health-1",
                name="Health",
                children=[
                    ApiCodingNode(
                        id="staff-1",
                        name="Staff",
                        children=[ApiCodingNode(id="supplies-1", name="Supplies")],
                    )
                ],
            ),
        ]
    )
    assert len(levels.root_codes) == 2
    assert levels.root_codes[0].id == "water-1"
    assert levels.root_codes[0].children[0].id == "dist-1"
    assert levels.root_codes[0].children[0].children[0].id == "wait-1"


def test_assigned_code_has_all_level_fields():
    """ApiAssignedCode must have all L1/L2/L3 id and name fields."""
    code = ApiAssignedCode(
        coding_level_1_id="type-1",
        coding_level_1_name="Type A",
        coding_level_2_id="cat-1",
        coding_level_2_name="Category A",
        coding_level_3_id="code-1",
        coding_level_3_name="Code A",
        confidence_type=0.9,
        confidence_category=0.8,
        confidence_code=0.7,
        confidence_aggregate=0.7,
        explanation="Test explanation",
    )
    assert code.coding_level_1_id == "type-1"
    assert code.coding_level_1_name == "Type A"
    assert code.coding_level_2_id == "cat-1"
    assert code.coding_level_2_name == "Category A"
    assert code.coding_level_3_id == "code-1"
    assert code.coding_level_3_name == "Code A"
