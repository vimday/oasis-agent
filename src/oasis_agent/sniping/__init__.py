"""Base launch sniper execution package.

Observation stays in ``oasis_agent.launch``; this package holds the
user-authorized execution path (monitoring, quoting, signing, broadcasting).
Signing material is held only inside the running process from the environment
and is never serialized, logged, or printed.
"""
