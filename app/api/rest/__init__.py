"""Client-facing governance REST mirror (B1 trust boundary).

The package is a thin transport layer over :mod:`app.api.rest.operations`.
Handlers only parse/validate the request, resolve the trusted principal and
delegate; they never re-implement PDP, PEP or task permission rules.  The same
operations module is used by the MCP tool surface in :mod:`app.api.mcp`.
"""
