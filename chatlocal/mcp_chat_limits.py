"""Shared default limits for the persistent-chat MCP surface.

Kept in its own module because mcp_chat imports mcp_chat_prompts, so the
prompt packets cannot define the numbers the tool schemas advertise.
"""
WAIT_TIMEOUT_SECONDS = 150
