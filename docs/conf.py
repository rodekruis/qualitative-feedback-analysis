"""Sphinx configuration for the qualitative-feedback-analysis docs.

Builds the prose docs under ``docs/`` together with auto-generated API docs
sourced from ``src/qfa``. Run via ``make docs`` at the repo root.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

# Make src/qfa importable so autodoc/autosummary can introspect the package
# even when Sphinx is run outside an editable install (e.g. on RTD / CI).
sys.path.insert(0, os.path.abspath(os.path.join("..", "src")))

import qfa

# -- Project information ----------------------------------------------------

project = "Qualitative Feedback Analysis"
copyright = f"{datetime.utcnow().year}, Marius Helf, Paul van Houtum"
author = "Marius Helf, Paul van Houtum"
release = qfa.__version__
version = qfa.__version__

# -- General configuration --------------------------------------------------

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "sphinxcontrib.mermaid",
]

source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

templates_path = ["_templates"]

exclude_patterns = [
    "_build",
    "_apidoc",
    "reviews",
    "architecture-review-*.md",
    "superpowers",
    "TODO-TOMORROW.md",
    "Thumbs.db",
    ".DS_Store",
    # README.md files in this tree are thin stubs that exist only as the
    # GitHub auto-rendered landing page when someone browses a folder URL
    # on github.com. The canonical section index for Sphinx is index.md.
    "README.md",
    "**/README.md",
]

# -- Napoleon ---------------------------------------------------------------

# Render docstring `Attributes` sections as :ivar: field lists rather than
# `.. py:attribute::` directives. The latter collides with autodoc's own
# attribute documentation (from real class-body annotations) and emits
# duplicate-object-description warnings. :ivar: is a non-indexing role,
# so the visual rendering is the same but no second Python-domain entry
# is registered.
napoleon_use_ivar = True

# -- MyST configuration -----------------------------------------------------

myst_enable_extensions = [
    "colon_fence",
    "deflist",
]

# Render ```mermaid fenced blocks via sphinxcontrib-mermaid rather than as
# plain code listings.
myst_fence_as_directive = ["mermaid"]

myst_heading_anchors = 3

# -- Autodoc / autosummary --------------------------------------------------

autodoc_member_order = "bysource"
autoclass_content = "both"
autosummary_generate = True
autosummary_imported_members = False
autodoc_default_options = {
    "members": True,
    "member-order": "bysource",
    "undoc-members": True,
    "show-inheritance": True,
    "exclude-members": "__weakref__,__dict__,__module__",
}

# -- Intersphinx ------------------------------------------------------------

# Only mappings we actually cross-reference today. Add pydantic / fastapi
# back the first time a doc role needs them; keeping unused inventories
# in the list means failed network fetches log warnings on every build.
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

# -- HTML output ------------------------------------------------------------

html_theme = "pydata_sphinx_theme"
html_static_path = ["_static"]
html_title = f"{project} {release}"
