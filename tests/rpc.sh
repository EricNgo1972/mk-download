#!/bin/bash
# rpc.sh TOOL JSON_ARGS  -> calls one MCP tool on $BASE (default localhost:8765) with $MCP_TOKEN
BASE=${BASE:-http://localhost:8765}
curl -s -X POST "$BASE/mcp" -H "Authorization: Bearer $MCP_TOKEN" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":${2:-{\}}}}" \
  | python3 -c '
import json, sys
d = json.load(sys.stdin)
if "error" in d: print("RPC ERROR", d["error"]); sys.exit(1)
r = d["result"]
if r.get("isError"): print("TOOL ERROR", r["content"][0]["text"]); sys.exit(2)
print(json.dumps(r.get("structuredContent") or r["content"], indent=1)[:4000])'
