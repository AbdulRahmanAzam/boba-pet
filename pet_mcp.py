"""MCP server that gives Claude Code (or any MCP client) the pet's computer tools.

The pet starts Claude Code with this server in --mcp-config and passes
CLAUDE_PET_BRIDGE="port:token". Every tool call is forwarded to the running
pet over 127.0.0.1 (computer.serve), so the pet's no-delete guard, approval
popups and captions apply. Speaks MCP over stdio: one JSON-RPC message per line.
"""

import json
import os
import socket
import sys

from computer import SPECS

SERVER = {"name": "claude-pet-pc", "version": "1.0"}
DEFAULT_VERSION = "2025-06-18"


def forward(name: str, args: dict) -> dict:
    port, token = os.environ["CLAUDE_PET_BRIDGE"].split(":", 1)
    with socket.create_connection(("127.0.0.1", int(port)), timeout=120) as sock:
        sock.sendall(json.dumps({"token": token, "name": name, "args": args}).encode()
                     + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(1 << 16)
            if not chunk:
                break
            data += chunk
    return json.loads(data)


def call_tool(params: dict) -> dict:
    try:
        out = forward(params.get("name", ""), params.get("arguments") or {})
    except (OSError, ValueError, KeyError) as exc:
        return {"content": [{"type": "text", "text": f"The pet is not reachable: {exc}"}],
                "isError": True}
    content = [{"type": "text", "text": out.get("text", "")}]
    if out.get("image"):
        content.append({"type": "image", "data": out["image"], "mimeType": "image/png"})
    return {"content": content}


def handle(msg: dict) -> dict | None:
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", DEFAULT_VERSION),
                "capabilities": {"tools": {}}, "serverInfo": SERVER}
    if method == "tools/list":
        return {"tools": [{"name": s["name"], "description": s["description"],
                           "inputSchema": s["parameters"]} for s in SPECS]}
    if method == "tools/call":
        return call_tool(params)
    if method == "ping":
        return {}
    return None


def main() -> None:
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "id" not in msg:  # notification
            continue
        result = handle(msg)
        reply = {"jsonrpc": "2.0", "id": msg["id"]}
        if result is None:
            reply["error"] = {"code": -32601, "message": f"Unknown method {msg.get('method')}"}
        else:
            reply["result"] = result
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
