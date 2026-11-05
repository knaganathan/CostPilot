"""CostPilot Copilot: a read-only, multi-turn chat about one AWS account.

Uses the same 14 read-only boto3 tools as the analysis agent, exposed through an in-process MCP
server to the local Claude Code. It cannot change anything in the account: there are no write tools,
and Claude Code's built-in tools (Bash, Read, Write, ...) are disabled.

Each chat is owned by one long-lived asyncio task, because the SDK client must be opened and closed
from the same task. The web layer only pushes user messages onto a queue and reads events back.
"""
import asyncio
import glob
import os

import boto3
from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, TextBlock, ToolUseBlock,
    create_sdk_mcp_server,
)

from agent import SERVER, to_mcp_tools
from tools import discover_regions, make_tools

MAX_REPORT_CHARS = 12000  # latest report given to the chat as background context

SYSTEM = """You are CostPilot Copilot, a read-only FinOps assistant for ONE AWS account (EC2, EBS, RDS, MSK, S3).

How to answer:
- Use the tools to get facts. Call only the tools needed for the question, not all of them.
- Be concise: lead with the answer, then the evidence (resource IDs, numbers from tool output).
- Never conclude a resource is idle, underused or oversized unless a tool shows status "validated_30d" (30 days of metrics). If a tool says "insufficient_data" or "not_validated", say you cannot judge it yet and why.
- Point-in-time checks (unattached volumes, gp2, snapshots, S3 configuration) have no usage history: say so, and suggest confirming with the owner.
- Label rough savings as estimates; do not invent numbers.
- You are READ-ONLY. You cannot change, stop, delete, resize or tag anything. If asked to apply a change, say that is not available in this version, and describe the change someone with the right access could make and what to check first. Never claim an action was taken.
- If a question is outside EC2, EBS, RDS, MSK, S3 or AWS cost, say it is out of scope.
- If a tool errors or is denied, say so plainly and answer with what you do have."""


def latest_report_context(account: str, reports_dir: str) -> str:
    files = sorted(glob.glob(os.path.join(reports_dir, f"costpilot-{account}-*.md")))
    if not files:
        return ""
    with open(files[-1], encoding="utf-8") as f:
        text = f.read()[:MAX_REPORT_CHARS]
    return (f"\n\nBackground: the latest analysis report for this account ({os.path.basename(files[-1])}). "
            f"It may be out of date; prefer fresh tool output when the user asks for current facts.\n\n{text}")


class ChatSession:
    def __init__(self, chat_id: str, profile: str, regions_arg: str, reports_dir: str):
        self.id, self.profile, self.regions_arg, self.reports_dir = chat_id, profile, regions_arg, reports_dir
        self.events: list[dict] = []
        self.status = "starting"  # starting | idle | busy | closed
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.task = asyncio.create_task(self._worker())

    def _emit(self, type_: str, text: str = ""):
        self.events.append({"type": type_, "text": text})

    def send(self, text: str):
        self.status = "busy"  # set now so a second message cannot slip in before the worker wakes
        self._queue.put_nowait(text)

    def close(self):
        self._queue.put_nowait(None)

    async def _worker(self):
        try:
            account = (await asyncio.to_thread(
                lambda: boto3.Session(profile_name=self.profile).client("sts").get_caller_identity()["Account"]))
            if self.regions_arg == "auto":
                regions = await asyncio.to_thread(discover_regions, self.profile)
            else:
                regions = [r.strip() for r in self.regions_arg.split(",")]
            beta_tools = make_tools(self.profile, regions)
            server = create_sdk_mcp_server(SERVER, tools=to_mcp_tools(beta_tools, log=lambda m: None))
            options = ClaudeAgentOptions(
                system_prompt=SYSTEM + latest_report_context(account, self.reports_dir),
                mcp_servers={SERVER: server},
                tools=[],  # no built-in Claude Code tools
                allowed_tools=[f"mcp__{SERVER}__{t.name}" for t in beta_tools],
                max_turns=25,
            )
            async with ClaudeSDKClient(options) as client:
                self._emit("ready", f"Connected to account {account} ({', '.join(regions)})")
                self.status = "idle"
                while True:
                    text = await self._queue.get()
                    if text is None:
                        break
                    self._emit("user", text)
                    try:
                        await client.query(text)
                        async for msg in client.receive_response():
                            if isinstance(msg, AssistantMessage):
                                for block in msg.content:
                                    if isinstance(block, ToolUseBlock):
                                        self._emit("tool", block.name.removeprefix(f"mcp__{SERVER}__"))
                                    elif isinstance(block, TextBlock) and block.text.strip():
                                        self._emit("text", block.text)
                            elif isinstance(msg, ResultMessage) and msg.is_error:
                                self._emit("error", f"{msg.subtype}: {msg.errors}")
                    except Exception as e:  # noqa: BLE001 - keep the chat alive after a failed turn
                        self._emit("error", str(e))
                    self._emit("done")
                    self.status = "idle"
        except Exception as e:  # noqa: BLE001 - startup failure (bad profile, expired login, ...)
            self._emit("error", str(e))
        finally:
            self.status = "closed"
