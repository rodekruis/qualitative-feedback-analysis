"""Tests for API schemas."""

import pytest
from pydantic import ValidationError

from qfa.api.schemas import CodingLevelsApi, CodingNodeApi


def test_max_child_depth_leaf_returns_zero():
    assert CodingNodeApi(name="a").max_child_depth() == 0


def test_max_child_depth_single_level():
    node = CodingNodeApi(name="root", children=[CodingNodeApi(name="child")])
    assert node.max_child_depth() == 1


def test_max_child_depth_two_levels():
    node = CodingNodeApi(
        name="root",
        children=[CodingNodeApi(name="mid", children=[CodingNodeApi(name="leaf")])],
    )
    assert node.max_child_depth() == 2


def test_max_child_depth_returns_deepest_branch():
    node = CodingNodeApi(
        name="root",
        children=[
            CodingNodeApi(name="shallow"),
            CodingNodeApi(name="deep", children=[CodingNodeApi(name="deeper")]),
        ],
    )
    assert node.max_child_depth() == 2


def test_min_child_depth_leaf_returns_zero():
    assert CodingNodeApi(name="a").min_child_depth() == 0


def test_min_child_depth_single_level():
    node = CodingNodeApi(name="root", children=[CodingNodeApi(name="child")])
    assert node.min_child_depth() == 1


def test_min_child_depth_two_levels():
    node = CodingNodeApi(
        name="root",
        children=[CodingNodeApi(name="mid", children=[CodingNodeApi(name="leaf")])],
    )
    assert node.min_child_depth() == 2


def test_min_child_depth_returns_shallowest_branch():
    node = CodingNodeApi(
        name="root",
        children=[
            CodingNodeApi(name="shallow"),
            CodingNodeApi(name="deep", children=[CodingNodeApi(name="deeper")]),
        ],
    )
    assert node.min_child_depth() == 1


def test_coding_levels_valid_flat_tree():
    levels = CodingLevelsApi(
        root_codes=[CodingNodeApi(name="a"), CodingNodeApi(name="b")]
    )
    assert len(levels.root_codes) == 2


def test_coding_levels_valid_uniform_depth():
    levels = CodingLevelsApi(
        root_codes=[
            CodingNodeApi(
                name="Water",
                children=[
                    CodingNodeApi(
                        name="Distribution",
                        children=[CodingNodeApi(name="Waiting times")],
                    )
                ],
            ),
            CodingNodeApi(
                name="Health",
                children=[
                    CodingNodeApi(
                        name="Staff",
                        children=[CodingNodeApi(name="Supplies")],
                    )
                ],
            ),
        ]
    )
    assert len(levels.root_codes) == 2


def test_coding_levels_unequal_depth_raises():
    with pytest.raises(ValueError, match="same depth"):
        CodingLevelsApi(
            root_codes=[
                CodingNodeApi(name="flat"),
                CodingNodeApi(name="deep", children=[CodingNodeApi(name="child")]),
            ]
        )


def test_coding_levels_unequal_depth_within_subtree_raises():
    with pytest.raises(ValueError, match="same depth"):
        CodingLevelsApi(
            root_codes=[
                CodingNodeApi(
                    name="root",
                    children=[
                        CodingNodeApi(name="shallow"),
                        CodingNodeApi(
                            name="deep",
                            children=[CodingNodeApi(name="deeper")],
                        ),
                    ],
                ),
            ]
        )


def test_coding_levels_with_no_children_fail():
    with pytest.raises(ValidationError, match="should have at least 1"):
        CodingLevelsApi(root_codes=[])
