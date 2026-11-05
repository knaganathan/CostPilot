"""CostPilot agent running on the locally installed Claude Code (uses your existing CLI login / SSO).

No ANTHROPIC_API_KEY needed. Claude Code's own auth and managed policy settings apply.
Only the CostPilot read-only AWS tools are exposed; Claude Code's built-in tools
(Bash, Read, Write, ...) are disabled so the model cannot touch anything else.

Usage (one or more profiles; each gets its own report named after its account ID):
    python agent.py --profile dev prod --out-dir reports

The same functions back the web UI (ui_server.py), which passes its own `log` callback.
"""
import argparse
import asyncio
import datetime as dt
import os
import sys
import time

import boto3
from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ToolUseBlock,
    create_sdk_mcp_server, query, tool,
)

from agent_api import SYSTEM  # same analyst instructions as the API-key version
from render import render as render_html
from tools import discover_regions, make_tools

SERVER = "costpilot"


def to_mcp_tools(beta_tools, log=print):
    """Wrap the boto3 tools (sync) as in-process MCP tools for Claude Code."""
    wrapped = []
    for t in beta_tools:
        def make(t):
            @tool(t.name, t.description, t.input_schema)
            async def handler(args):
                started = time.monotonic()
                try:
                    text = await asyncio.to_thread(t.call, args)  # boto3 is blocking
                    log(f"    done: {t.name} ({time.monotonic() - started:.0f}s)")
                    return {"content": [{"type": "text", "text": str(text)}]}
                except Exception as e:  # noqa: BLE001 - surface to the model, don't crash
                    log(f"    failed: {t.name} ({time.monotonic() - started:.0f}s): {e}")
                    return {"content": [{"type": "text", "text": f"error: {e}"}], "is_error": True}
            return handler
        wrapped.append(make(t))
    return wrapped


async def analyze(profile: str, regions_arg: str, model: str | None, log=print) -> tuple[str, str]:
    """Analyze one profile. Returns (account_id, markdown_report)."""
    account = boto3.Session(profile_name=profile).client("sts").get_caller_identity()["Account"]
    if regions_arg == "auto":
        regions = discover_regions(profile)
    else:
        regions = [r.strip() for r in regions_arg.split(",")]
    log(f"[{profile}] account {account}, {len(regions)} region(s): {', '.join(regions)}")
    log(f"[{profile}] scanning can take several minutes; tool lines appear when Claude calls a tool, "
        f"not when it finishes")

    beta_tools = make_tools(profile, regions)
    server = create_sdk_mcp_server(SERVER, tools=to_mcp_tools(beta_tools, log))
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM,
        mcp_servers={SERVER: server},
        tools=[],  # no built-in Claude Code tools
        allowed_tools=[f"mcp__{SERVER}__{t.name}" for t in beta_tools],
        max_turns=40,
        **({"model": model} if model else {}),
    )
    report = ""
    async for msg in query(
        prompt="Analyze this account and produce the cost optimization summary.",
        options=options,
    ):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    log(f"[{profile}] tool: {block.name.removeprefix(f'mcp__{SERVER}__')}")
                elif isinstance(block, TextBlock):
                    report = block.text  # last text block is the final report
                    log(f"[{profile}] claude: {block.text[:90].strip()!r}...")
        elif isinstance(msg, ResultMessage):
            if msg.is_error:
                raise RuntimeError(f"agent failed: {msg.subtype} {msg.errors}")
            report = msg.result or report
    header = (f"> **AWS account:** `{account}` &middot; **profile:** `{profile}` &middot; "
              f"**regions:** {', '.join(regions)}\n\n")
    return account, header + report


async def analyze_to_files(profile: str, regions_arg: str, model: str | None,
                           out_dir: str, log=print) -> tuple[str, str]:
    """Analyze one profile and write costpilot-<account>-<date>.md/.html. Returns (account_id, base_path)."""
    account, report = await analyze(profile, regions_arg, model, log)
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, f"costpilot-{account}-{dt.date.today().strftime('%Y%m%d')}")
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(report)
    with open(base + ".html", "w", encoding="utf-8") as f:
        f.write(render_html(report))
    log(f"[{profile}] wrote {base}.md and {base}.html")
    return account, base


async def run(args):
    results = []
    for profile in args.profile:  # sequential: one account at a time
        try:
            account, base = await analyze_to_files(
                profile, args.regions, args.model, args.out_dir, log=lambda m: print(m, flush=True))
            results.append((profile, account, base))
        except Exception as e:  # noqa: BLE001 - one bad profile must not stop the others
            print(f"[{profile}] FAILED: {e}", flush=True)
            results.append((profile, None, str(e)))

    sys.stdout.reconfigure(encoding="utf-8")
    print("\nSummary")
    for profile, account, info in results:
        print(f"  {profile}: " + (f"account {account} -> {info}.html" if account else f"FAILED ({info})"))
    if any(a is None for _, a, _ in results):
        raise SystemExit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, nargs="+",
                    help="one or more AWS CLI profiles (read-only); each is analyzed as its own account")
    ap.add_argument("--regions", default="auto",
                    help="comma-separated regions, or 'auto' (default): regions with in-scope spend "
                         "in the last 60 days (applied per profile)")
    ap.add_argument("--model", help="override Claude Code's default model")
    ap.add_argument("--out-dir", default="reports",
                    help="folder for reports, named costpilot-<account-id>-<date>.md/.html (default: reports)")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
