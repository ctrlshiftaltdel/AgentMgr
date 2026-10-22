"""Client MCP governance surface (B1 trust boundary).

The official ``mcp`` Python SDK provides the Streamable HTTP transport and the
JSON-RPC/protocol implementation.  This package only registers tools and adapts
each call to :mod:`app.api.rest.operations`; it is not a hand-written protocol
implementation.  Server-side Agent business tools (B2) are a separate surface
and are not exposed here.
"""
