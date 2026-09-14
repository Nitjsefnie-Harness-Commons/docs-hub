"""docs-hub CLI — the client half of docs.nitjsefni.eu.

The server in `backend/` and this client are one contract: a change to an
endpoint here is a change to the endpoint there, and keeping them in one
repository is what lets a single test run prove both halves still agree.

The fleet setup bundle installs this as a wheel and keeps a thin wrapper at
`~/.agent-bundle/scripts/docs_hub.py`, so the documented absolute-path
invocation keeps working unchanged.
"""

__version__ = "0.2.0"
