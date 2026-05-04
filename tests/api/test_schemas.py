"""Tests for API schemas."""

import pytest
from pydantic import ValidationError

from qfa.api.schemas import ApiCodingLevels, ApiCodingNode


def test_max_child_depth_leaf_returns_zero():
    assert ApiCodingNode(name="a").max_child_depth() == 0


def test_max_child_depth_single_level():
    node = ApiCodingNode(name="root", children=[ApiCodingNode(name="child")])
    assert node.max_child_depth() == 1


def test_max_child_depth_two_levels():
    node = ApiCodingNode(
        name="root",
        children=[ApiCodingNode(name="mid", children=[ApiCodingNode(name="leaf")])],
    )
    assert node.max_child_depth() == 2


def test_max_child_depth_returns_deepest_branch():
    node = ApiCodingNode(
        name="root",
        children=[
            ApiCodingNode(name="shallow"),
            ApiCodingNode(name="deep", children=[ApiCodingNode(name="deeper")]),
        ],
    )
    assert node.max_child_depth() == 2


def test_min_child_depth_leaf_returns_zero():
    assert ApiCodingNode(name="a").min_child_depth() == 0


def test_min_child_depth_single_level():
    node = ApiCodingNode(name="root", children=[ApiCodingNode(name="child")])
    assert node.min_child_depth() == 1


def test_min_child_depth_two_levels():
    node = ApiCodingNode(
        name="root",
        children=[ApiCodingNode(name="mid", children=[ApiCodingNode(name="leaf")])],
    )
    assert node.min_child_depth() == 2


def test_min_child_depth_returns_shallowest_branch():
    node = ApiCodingNode(
        name="root",
        children=[
            ApiCodingNode(name="shallow"),
            ApiCodingNode(name="deep", children=[ApiCodingNode(name="deeper")]),
        ],
    )
    assert node.min_child_depth() == 1


def test_coding_levels_valid_flat_tree():
    levels = ApiCodingLevels(
        root_codes=[ApiCodingNode(name="a"), ApiCodingNode(name="b")]
    )
    assert len(levels.root_codes) == 2


def test_coding_levels_valid_uniform_depth():
    levels = ApiCodingLevels(
        root_codes=[
            ApiCodingNode(
                name="Water",
                children=[
                    ApiCodingNode(
                        name="Distribution",
                        children=[ApiCodingNode(name="Waiting times")],
                    )
                ],
            ),
            ApiCodingNode(
                name="Health",
                children=[
                    ApiCodingNode(
                        name="Staff",
                        children=[ApiCodingNode(name="Supplies")],
                    )
                ],
            ),
        ]
    )
    assert len(levels.root_codes) == 2


def test_coding_levels_unequal_depth_raises():
    with pytest.raises(ValueError, match="same depth"):
        ApiCodingLevels(
            root_codes=[
                ApiCodingNode(name="flat"),
                ApiCodingNode(name="deep", children=[ApiCodingNode(name="child")]),
            ]
        )


def test_coding_levels_unequal_depth_within_subtree_raises():
    with pytest.raises(ValueError, match="same depth"):
        ApiCodingLevels(
            root_codes=[
                ApiCodingNode(
                    name="root",
                    children=[
                        ApiCodingNode(name="shallow"),
                        ApiCodingNode(
                            name="deep",
                            children=[ApiCodingNode(name="deeper")],
                        ),
                    ],
                ),
            ]
        )


def test_coding_levels_with_no_children_fail():
    with pytest.raises(ValidationError, match="should have at least 1"):
        ApiCodingLevels(root_codes=[])
