"""ATS-related services that belong to the `app/` layer.

Distinct from `automation/ats/`, which holds the browser-driving adapters. The
split follows the same boundary as the rest of the project: `app/` owns
persistence and API concerns, `automation/` owns the browser.
"""
