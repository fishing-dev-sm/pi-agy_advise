"""Expose the Antigravity CLI streaming protocol as a local Codex tool."""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import anyio
from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from project import ProjectEvent, ProjectStore, default_db, handle_project
from telemetry import record_invocation


IDLE_SECONDS = 600
Effort = Literal["low", "medium", "high", "xhigh", "max"]
Stance = Literal["global", "red_team", "tactical"]
def workspace() -> str:
    """Directory the advisor CLI may inspect. Defaults to the launch directory."""
    configured = os.getenv("AGY_ADVISE_WORKSPACE", "").strip()
    return str(Path(configured).expanduser().resolve()) if configured else os.getcwd()


SESSION_SETTINGS = {
    "toolPermission": "always-proceed",
    "allowNonWorkspaceAccess": False,
    "permissions": {
        "deny": [
            "command(rm)",
            "command(sudo)",
            "command(dd)",
            "command(chmod)",
            "command(chown)",
            "command(mkfs)",
            "command(shutdown)",
        ],
    },
}


def build_advice_prompt(
    question: str,
    context: str | None,
    stance: Stance,
    *,
    is_followup: bool,
) -> str:
    """Build a domain-agnostic, global-first advisor prompt."""
    sections = []
    if is_followup:
        sections.append(
            "Continue this independent review from the new evidence supplied below. "
            "Revise earlier conclusions if the new facts contradict them, and keep separating "
            "confirmed facts, inferences, and missing facts."
        )
    else:
        sections.append(
            "Ground the answer in evidence: read the relevant files and run read-only commands "
            "instead of guessing from the question alone. Never claim a file or fact you have not "
            "actually examined; if critical evidence is missing, name it explicitly."
        )

    sections.append(f"Question:\n{question}")
    if context:
        sections.append(f"Context:\n{context}")

    if stance == "red_team":
        sections.append(
            "Stance (Red-Team Audit): Construct the strongest evidence-based case for why the proposed "
            "approach, architecture, or hypothesis will fail to achieve the real end goal. Identify hidden "
            "assumptions, proxy-vs-real-goal traps, and single points of failure. Name the cheapest "
            "falsifying check and state clearly whether this branch should stop or pivot."
        )
    elif stance == "tactical":
        sections.append(
            "Stance (Focused Tactical): Answer directly from first principles for the stated scope. "
            "Still flag any glaring premise flaw or contradiction in the evidence, distinguish confirmed "
            "facts from assumptions, and state concrete pass/stop criteria."
        )
    elif is_followup:
        sections.append(
            "Stance (Independent Reviewer): Do not rubber-stamp the proposed options. Derive the right "
            "direction from first principles using the goal, hard constraints, and observed failures, "
            "and say why."
        )
    else:
        sections.append(
            "Stance (Global-First Independent Reviewer):\n"
            "1. Zoom out before zooming in: first check whether the question frames a local symptom or "
            "sunk-cost patch rather than the real main bottleneck against the overarching goal.\n"
            "2. Do not rubber-stamp proposed options: derive the right direction from first principles "
            "using the goal, hard constraints, and observed failures. You are explicitly authorized to "
            "challenge the premise or recommend stopping/pivoting the current branch.\n"
            "3. Separate confirmed facts, inferences, and critical unknowns that could overturn your judgment.\n"
            "4. Give a decisive recommendation, the single highest-leverage next step, and a concrete "
            "falsification or stop condition. Match the language of the question."
        )
    return "\n\n".join(sections)


class Advisor:
    def __init__(self, executable: str, directory: str):
        self.executable = executable
        self.directory = directory
        self.lock = asyncio.Lock()
        self.conversations: dict[str, str] = {}
        self.session_efforts: dict[str, Effort] = {}
        self.session_models: dict[str, str | None] = {}
        self.active_session: str | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.idle: asyncio.Task[None] | None = None
        self.stderr_task: asyncio.Task | None = None
        self.stderr_tail = b""

    async def drain_stderr(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(4096):
            self.stderr_tail = (self.stderr_tail + chunk)[-4096:]

    def cancel_idle(self) -> None:
        if self.idle is not None:
            self.idle.cancel()
            self.idle = None

    async def expire(self) -> None:
        await asyncio.sleep(IDLE_SECONDS)
        async with self.lock:
            self.idle = None
            await self.stop(graceful=True)

    @staticmethod
    async def drain_and_wait(process: asyncio.subprocess.Process) -> None:
        while await process.stdout.read(65536):
            pass
        await process.wait()

    async def stop(self, *, graceful: bool = False) -> None:
        self.cancel_idle()
        process = self.process
        if process is None:
            return
        process.stdin.close()
        if graceful and process.returncode is None:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.drain_and_wait(process), 1)
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(self.drain_and_wait(process), 2)
            except asyncio.TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.drain_and_wait(process), 2)
        if self.stderr_task is not None:
            self.stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.stderr_task
            self.stderr_task = None
        self.process = None
        self.active_session = None

    async def start(self, session_id: str, effort: Effort | None = None, model: str | None = None) -> None:
        await self.stop()
        # The CLI's headless default avoids Plan mode's artifact approval step.
        arguments = [
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--sandbox",
            "--settings", json.dumps(SESSION_SETTINGS),
        ]
        if effort is not None:
            arguments.extend(["--effort", effort])
        if model is not None:
            arguments.extend(["--model", model])
        if conversation := self.conversations.get(session_id):
            arguments.extend(["--conversation", conversation])
        self.stderr_tail = b""
        self.process = await asyncio.create_subprocess_exec(
            self.executable,
            *arguments,
            cwd=self.directory,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=4 * 1024 * 1024,
        )
        self.active_session = session_id
        self.stderr_task = asyncio.create_task(self.drain_stderr(self.process.stderr))

    def failure(self, message: str, detail: object = "") -> RuntimeError:
        diagnostic = f"{detail} {self.stderr_tail.decode(errors='replace')}".lower()
        if any(term in diagnostic for term in ("sign in", "sign-in", "not logged in", "authenticat")):
            return RuntimeError("Antigravity CLI needs an authenticated session. Sign in with agy on this host.")
        if any(term in diagnostic for term in ("quota", "resource_exhausted", "rate limit")):
            return RuntimeError("Antigravity reported a quota or rate limit.")
        return RuntimeError(message)

    async def exchange(
        self, prompt: str, session_id: str, effort: Effort | None, model: str | None
    ) -> dict[str, object]:
        process = self.process
        if (
            process is None
            or self.active_session != session_id
            or process.returncode is not None
            or process.stdin.is_closing()
        ):
            await self.start(session_id, effort, model)
            process = self.process
        else:
            self.stderr_tail = b""
        message = {"event": "user", "message": {"content": prompt}}
        try:
            process.stdin.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as error:
            process.stdin.close()
            with suppress(BrokenPipeError, ConnectionResetError, asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(process.stdin.wait_closed()), 0.25)
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(self.stderr_task), 0.25)
            raise self.failure("Antigravity CLI closed its input before receiving the question.") from error
        while line := await process.stdout.readline():
            try:
                event = json.loads(line)
            except (UnicodeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            if event.get("event") == "init":
                if isinstance(event.get("conversation_id"), str):
                    self.conversations[session_id] = event["conversation_id"]
            if event.get("event") != "result":
                continue
            result = event.get("result")
            if not isinstance(result, dict):
                raise self.failure("Antigravity returned an invalid result.")
            conversation = result.get("conversation_id") or event.get("conversation_id")
            if isinstance(conversation, str):
                self.conversations[session_id] = conversation
            if result.get("status") != "SUCCESS":
                raise self.failure("Antigravity reported a failed turn.", result.get("error"))
            advice = result.get("response")
            if not isinstance(advice, str) or not advice.strip():
                raise self.failure(
                    "Antigravity returned no advice. A tool likely needed approval that headless mode "
                    "cannot prompt for; check toolPermission in settings.json."
                )
            if session_id not in self.conversations:
                raise self.failure("Antigravity returned advice without a conversation ID.")
            return {
                "advice": advice.strip(),
                "session_id": session_id,
                "duration_seconds": result.get("duration_seconds"),
                "usage": result.get("usage"),
                "model_reported": result.get("model") if isinstance(result.get("model"), str) else None,
            }
        await process.wait()
        await self.stderr_task
        raise self.failure(f"Antigravity CLI exited before replying (exit {process.returncode}).")

    async def ask(
        self,
        prompt: str,
        session_id: str | None,
        timeout: int,
        effort: Effort | None = None,
        model: str | None = None,
    ) -> dict[str, object]:
        clock = asyncio.get_running_loop().time
        started = clock()
        queue_timeout = f"Antigravity timed out after {timeout} seconds waiting for the previous advisor turn."
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout)
        except asyncio.TimeoutError as error:
            raise RuntimeError(queue_timeout) from error

        try:
            queue_seconds = clock() - started
            remaining = timeout - queue_seconds
            if remaining <= 0:
                raise RuntimeError(queue_timeout)
            if session_id is not None:
                if session_id not in self.conversations:
                    raise ValueError("Unknown advisor session. Omit session_id to start a new conversation.")
                saved_effort = self.session_efforts.get(session_id)
                if effort is not None and effort != saved_effort:
                    raise ValueError("Omit session_id to start a conversation with a different reasoning effort.")
                effort = saved_effort
                saved_model = self.session_models[session_id]
                if model is not None and model != saved_model:
                    raise ValueError("Omit session_id to start a conversation with a different model.")
                model = saved_model
            else:
                model = model or os.getenv("AGY_ADVISE_MODEL") or None
            self.cancel_idle()
            key = session_id or uuid4().hex
            try:
                reply = await asyncio.wait_for(self.exchange(prompt, key, effort, model), remaining)
            except BaseException as error:
                with anyio.CancelScope(shield=True):
                    await self.stop()
                if session_id is None:
                    self.conversations.pop(key, None)
                if isinstance(error, asyncio.TimeoutError):
                    raise RuntimeError(f"Antigravity timed out after {timeout} seconds, including queue wait.") from error
                raise
            if effort is not None:
                self.session_efforts[key] = effort
            self.session_models[key] = model
            self.idle = asyncio.create_task(self.expire())
            reply.update(
                queue_seconds=round(queue_seconds, 3),
                elapsed_seconds=round(clock() - started, 3),
                requested_effort=effort,
                model_requested=model,
            )
            return reply
        finally:
            self.lock.release()


def _classify_response_status(response: dict[str, object]) -> str:
    review_status = response.get("review_status")
    if review_status == "transport_error":
        error_text = str(response.get("error") or "").lower()
        return "timeout" if "timed out" in error_text else "error"
    if review_status == "invalid_decision":
        return "error"
    return "ok"


def _classify_exception_status(error: BaseException) -> str:
    if isinstance(error, ValueError):
        return "validation_error"
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if "timed out" in str(error).lower():
        return "timeout"
    return "error"


async def execute_advise(
    *,
    store: ProjectStore,
    advisor: Advisor,
    question: str = "",
    context: str | None = None,
    session_id: str | None = None,
    timeout_seconds: int = 240,
    effort: Effort | None = None,
    model: str | None = None,
    stance: Stance | None = None,
    project: ProjectEvent | None = None,
) -> dict[str, object]:
    """Execute one agy_advise call and append a lightweight local audit record."""
    clock = asyncio.get_running_loop().time
    started = clock()
    invocation = {"id": uuid4().hex, "tool": "agy_advise", "transport": "mcp"}
    is_project = project is not None
    mode = "project" if is_project else "qa"
    action = project.action if is_project else ("followup" if session_id is not None else "ask")
    resolved_stance: Stance | None = None if is_project else (stance or "global")
    project_id = getattr(project, "id", None) if is_project else None
    phase = getattr(project, "phase", None) if is_project else None

    response: dict[str, object] | None = None
    status = "ok"
    error_text: str | None = None
    try:
        if project is not None:
            if session_id is not None:
                raise ValueError("Project reviews use durable evidence and a fresh advisor session.")
            if stance is not None:
                raise ValueError("Project reviews use their own structured clean-room review protocol.")
            response = await handle_project(
                store, project, advisor, invocation, question, context, timeout_seconds, effort, model
            )
            status = _classify_response_status(response)
            return response

        if not question.strip():
            raise ValueError("Supply a self-contained question.")
        prompt = build_advice_prompt(question, context, resolved_stance or "global", is_followup=session_id is not None)
        reply = await advisor.ask(prompt, session_id, timeout_seconds, effort, model)
        response = {"invocation": invocation, "requested_stance": resolved_stance, **reply}
        return response
    except BaseException as error:
        status = _classify_exception_status(error)
        error_text = str(error) or type(error).__name__
        raise
    finally:
        elapsed = clock() - started
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(
                lambda: record_invocation(
                    store.path,
                    invocation_id=invocation["id"],
                    mode=mode,
                    action=action,
                    status=status,
                    elapsed_seconds=elapsed,
                    question=question,
                    context=context,
                    stance=resolved_stance,
                    is_followup=session_id is not None,
                    session_id=session_id,
                    effort=effort,
                    model_requested=model,
                    project_id=project_id,
                    phase=phase,
                    response=response,
                    error=error_text,
                )
            )


def server(executable: str) -> FastMCP:
    store = ProjectStore(default_db())

    @asynccontextmanager
    async def lifespan(_server):
        advisor = Advisor(executable, workspace())
        try:
            yield advisor
        finally:
            with anyio.CancelScope(shield=True):
                async with advisor.lock:
                    await advisor.stop()

    mcp = FastMCP("Antigravity Advisor", lifespan=lifespan)

    @mcp.tool()
    async def agy_advise(
        ctx: Context,
        question: Annotated[str, Field(description="Self-contained question for Antigravity.")] = "",
        context: Annotated[str | None, Field(description="Minimal structured facts authorized for sharing with Google.")] = None,
        session_id: Annotated[str | None, Field(description="Previous ID for a follow-up. Omit to start fresh.")] = None,
        timeout_seconds: Annotated[int, Field(ge=1, le=240, description="Turn budget including any queue wait.")] = 240,
        effort: Annotated[
            Effort | None,
            Field(description="Reasoning effort for a new conversation. Omit for the CLI default or to inherit on a follow-up."),
        ] = None,
        model: Annotated[str | None, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")] = None,
        stance: Annotated[
            Stance | None,
            Field(description="Advisor stance for Q&A: global (default), red_team, or tactical."),
        ] = None,
        project: ProjectEvent | None = None,
    ) -> dict[str, object]:
        """Ask Antigravity for independent global judgment or manage a persistent project phase.

        Default Q&A uses a global-first stance across any project (architecture, debugging,
        systems, ML, product) and audits premises before answering local details.
        Project actions are init, report, status, list, memory, review and activate, backed
        by bounded three-tier external memory (L1 constitution, L2 distilled facts/postmortems,
        L3 recent window) and clean-room reviews with actionable HOLD prescriptions.
        A report that triggers review sends recorded project evidence to Google. Supply only
        text authorized for sharing.
        """
        advisor = ctx.request_context.lifespan_context
        return await execute_advise(
            store=store,
            advisor=advisor,
            question=question,
            context=context,
            session_id=session_id,
            timeout_seconds=timeout_seconds,
            effort=effort,
            model=model,
            stance=stance,
            project=project,
        )

    return mcp


if __name__ == "__main__":
    server(sys.argv[1]).run()
