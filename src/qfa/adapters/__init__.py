"""Driven adapter implementations.

Adapters in this package implement driven ports declared in
``qfa.domain.ports`` and are the only modules in the codebase that
import third-party infrastructure libraries (LLM SDKs, anonymisation
engines, etc.). The application service layer (``qfa.services``) and
domain (``qfa.domain``) depend on the ports, never on these
implementations directly.
"""
