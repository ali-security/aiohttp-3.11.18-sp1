"""Vendored compression backends bundled with aiohttp.

This package ships private copies of ``Brotli`` (CPython) and ``brotlicffi``
(other implementations) at version >= 1.2, which adds an output-size limit to
the streaming decompressor (``output_buffer_limit`` / ``max_length``). aiohttp
uses these copies to cap brotli decompression output (CVE-2025-69223) WITHOUT
forcing consumers to upgrade their declared ``Brotli`` / ``brotlicffi``
dependency.

The copies are imported only under their fully-qualified namespaced names
(``aiohttp._vendored.brotli`` / ``aiohttp._vendored.brotlicffi``) so they get
their own ``sys.modules`` entries and never shadow, replace, or collide with a
top-level ``brotli`` / ``brotlicffi`` the user may already have imported.
"""
