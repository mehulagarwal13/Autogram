"""Job-board connectors.

Each module here adapts one external source (Adzuna today) into the common
shape `job_ingestion` expects, and registers itself in `registry.py`. Added as
a real package rather than relying on Python 3 namespace packages, so this
directory behaves like every other package in the tree for tooling that walks
`__init__.py` (coverage, packaging, some import hooks).
"""
