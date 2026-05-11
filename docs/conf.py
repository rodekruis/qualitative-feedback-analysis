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

# qfa.domain re-exports symbols from its submodules so callers can import
# them from the package root. autodoc then sees each symbol twice (once
# under the canonical module, once under the re-export) and warns. The
# re-exports are intentional, so silence the noise.
suppress_warnings = ["ref.python"]

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

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pydantic": ("https://docs.pydantic.dev/latest/", None),
    "fastapi": ("https://fastapi.tiangolo.com/", None),
}

# -- HTML output ------------------------------------------------------------

html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]
html_title = f"{project} {release}"
