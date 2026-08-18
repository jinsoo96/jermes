"""Host-specific adapters. **Nothing in `jermes` core imports this package.**

The core (`ledger.py`, `agent.py`, `tools.py`, ...) stays provider-agnostic -
that discipline is what let `mcp_server.py` and this package both wrap the same
core without the core knowing either exists. A submodule here may import a
host's package only inside functions/methods (never at module top), guarded so
`import jermes` never requires that host to be installed.
"""
