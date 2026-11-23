import json
import os
import sys

import httpx

API = os.environ["API_URL"]
ROUTE = os.environ["GATEWAY_URL"] + "/mcp/tracker"
GRANTED = {
    "tracker-read": {"get_item", "get_comments", "get_related"},
    "tracker-write": {"comment", "ask_person", "create_subitem"},
}
TOOLS = set().union(*GRANTED.values())
failures = []


def check(step, ok, detail):
    print(f"{step}: {'ok' if ok else 'FAILED'} ({detail})", flush=True)
    if not ok:
        failures.append(step)


def observe(step, detail):
    print(f"{step}: observed ({detail})", flush=True)


def body_of(response):
    # Streamable HTTP answers a request as JSON or as one server-sent event.
    text = response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = data[-1] if data else ""
    try:
        return json.loads(text)
    except ValueError:
        return None


def rpc(client, method, params=None, token=None, session=None, ident=1):
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session:
        headers["Mcp-Session-Id"] = session
    message = {"jsonrpc": "2.0", "method": method}
    if ident is not None:
        message["id"] = ident
    if params is not None:
        message["params"] = params
    return client.post(ROUTE, headers=headers, json=message)


with httpx.Client(timeout=15) as client:
    # No definitions version: the token has no grants, which is what the gateway
    # is asked about below. A token with a grant needs an agent definition that
    # names one.
    issued = client.post(
        f"{API}/v1/agent-tokens",
        headers={"Authorization": f"Bearer {os.environ['SERVICE_TOKEN']}"},
        json={
            "tenant": "default",
            "app": "smoke",
            "project": "matchmark",
            "agent": "proof",
            "task": "tracker-mcp-proof",
            "attempt": 1,
            "model": "claude-sonnet-4-5",
        },
    )
    check("1 token issued", issued.status_code == 201, f"status {issued.status_code}")
    if issued.status_code != 201:
        sys.exit(1)
    token = issued.json()["token"]

    bare = rpc(client, "tools/list")
    check("2 tools/list, no token", bare.status_code == 401, f"status {bare.status_code}")

    wrong = rpc(client, "tools/list", token="not-a-token")
    check("3 tools/list, a token that is not ours", wrong.status_code == 401, f"status {wrong.status_code}")

    started = rpc(client, "initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "tracker-mcp-proof", "version": "1"},
    }, token=token)
    session = started.headers.get("mcp-session-id")
    answer = body_of(started)
    server = ((answer or {}).get("result") or {}).get("serverInfo")
    check(
        "4 initialize, the run's token",
        started.status_code == 200 and server is not None,
        f"status {started.status_code}, server {server}, session {'kept' if session else 'none'}",
    )
    if session:
        rpc(client, "notifications/initialized", token=token, session=session, ident=None)

    listed = rpc(client, "tools/list", token=token, session=session, ident=2)
    answer = body_of(listed)
    tools = (answer or {}).get("result", {}).get("tools")
    names = sorted(tool["name"] for tool in tools) if isinstance(tools, list) else None
    check(
        "5 tools/list, the run's token",
        listed.status_code == 200 and names is not None,
        f"status {listed.status_code}, tools {names}",
    )

    # The token has no grant. Hidden is the gateway authorizing from the claim
    # (or denying everything, which a token with a grant tells apart); every
    # tool listed is the gateway checking the token alone.
    if names is not None:
        if not names:
            observe("6 what the gateway lists to a token with no grant", "no tool: the gateway hides what is not granted")
        elif set(names) == TOOLS:
            observe("6 what the gateway lists to a token with no grant", "every tool: the gateway checks the token alone and the API refuses")
        else:
            observe("6 what the gateway lists to a token with no grant", f"some tools: {names}")

    called = rpc(client, "tools/call", {"name": "get_item", "arguments": {}}, token=token, session=session, ident=3)
    answer = body_of(called)
    result = (answer or {}).get("result")
    refused_by_api = (
        called.status_code == 200
        and isinstance(result, dict)
        and result.get("isError") is True
    )
    refused_by_gateway = called.status_code != 200 or (answer or {}).get("error") is not None
    detail = f"status {called.status_code}, {called.text[:300]!r}"
    check("7 tools/call get_item, no grant: refused", refused_by_api or refused_by_gateway, detail)
    if refused_by_gateway:
        observe("7 who refused it", "the gateway")
    elif refused_by_api:
        observe("7 who refused it", "the API, as a tool error the agent reads")

sys.exit(1 if failures else 0)
