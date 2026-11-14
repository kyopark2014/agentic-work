# Canva skills

The `canva-*` skills under `runtime_agent/langgraph/skills/` are adapted from
https://github.com/canva-sdks/canva-skills (Apache License 2.0, see `LICENSE`).

Changes: the `Canva:` namespace prefix was removed from MCP tool names, because
agentic-work registers MCP tools without a server prefix (for example
`Canva:start-editing-transaction` became `start-editing-transaction`).
The `canva-cli` skill was not imported (it targets the Canva CLI, not the MCP server).
