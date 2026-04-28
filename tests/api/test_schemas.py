"""Tests for API schemas."""

import pytest
from pydantic import ValidationError

from qfa.api.schemas import CodingLevels, CodingNode


def test_max_child_depth_leaf_returns_zero():
    assert CodingNode(name="a").max_child_depth() == 0


def test_max_child_depth_single_level():
    node = CodingNode(name="root", children=[CodingNode(name="child")])
    assert node.max_child_depth() == 1


def test_max_child_depth_two_levels():
    node = CodingNode(
        name="root",
        children=[CodingNode(name="mid", children=[CodingNode(name="leaf")])],
    )
    assert node.max_child_depth() == 2


def test_max_child_depth_returns_deepest_branch():
    node = CodingNode(
        name="root",
        children=[
            CodingNode(name="shallow"),
            CodingNode(name="deep", children=[CodingNode(name="deeper")]),
        ],
    )
    assert node.max_child_depth() == 2


def test_min_child_depth_leaf_returns_zero():
    assert CodingNode(name="a").min_child_depth() == 0


def test_min_child_depth_single_level():
    node = CodingNode(name="root", children=[CodingNode(name="child")])
    assert node.min_child_depth() == 1


def test_min_child_depth_two_levels():
    node = CodingNode(
        name="root",
        children=[CodingNode(name="mid", children=[CodingNode(name="leaf")])],
    )
    assert node.min_child_depth() == 2


def test_min_child_depth_returns_shallowest_branch():
    node = CodingNode(
        name="root",
        children=[
            CodingNode(name="shallow"),
            CodingNode(name="deep", children=[CodingNode(name="deeper")]),
        ],
    )
    assert node.min_child_depth() == 1


def test_coding_levels_valid_flat_tree():
    levels = CodingLevels(root_codes=[CodingNode(name="a"), CodingNode(name="b")])
    assert len(levels.root_codes) == 2


def test_coding_levels_valid_uniform_depth():
    levels = CodingLevels(
        root_codes=[
            CodingNode(
                name="Water",
                children=[
                    CodingNode(
                        name="Distribution", children=[CodingNode(name="Waiting times")]
                    )
                ],
            ),
            CodingNode(
                name="Health",
                children=[
                    CodingNode(name="Staff", children=[CodingNode(name="Supplies")])
                ],
            ),
        ]
    )
    assert len(levels.root_codes) == 2


def test_coding_levels_unequal_depth_raises():
    with pytest.raises(ValueError, match="same depth"):
        CodingLevels(
            root_codes=[
                CodingNode(name="flat"),
                CodingNode(name="deep", children=[CodingNode(name="child")]),
            ]
        )


def test_coding_levels_unequal_depth_within_subtree_raises():
    with pytest.raises(ValueError, match="same depth"):
        CodingLevels(
            root_codes=[
                CodingNode(
                    name="root",
                    children=[
                        CodingNode(name="shallow"),
                        CodingNode(name="deep", children=[CodingNode(name="deeper")]),
                    ],
                ),
            ]
        )


def test_coding_levels_with_no_children_fail():
    with pytest.raises(ValidationError, match="should have at least 1"):
        CodingLevels(root_codes=[])
