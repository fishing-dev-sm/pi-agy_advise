// Antigravity Advisor — bridge to the local `agy` CLI.
// One long-lived `agy` stream-json session, exposed to Pi as the agy_advise tool.
// The agy process keeps its own conversation and prompt cache; Pi never resends
// full history — only the question plus optional minimal context. Requires `agy`
// on PATH and the permission rules in ~/.gemini/antigravity-cli/settings.json.
// Permission bypass requires explicit user authorization.

import { Type } from "typebox";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const IDLE_MS = 10 * 60 * 1000;
const KILL_GRACE_MS = 3_000;
const MODEL_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$/;

type Stance = "global" | "red_team" | "tactical";
type Effort = "low" | "medium" | "high" | "xhigh" | "max";

type Advice = {
  response: string;
  conversationId?: string;
  seconds?: number;
  usage?: unknown;
  model?: string;
  queueSeconds?: number;
  elapsedSeconds?: number;
};

type AskOptions = {
  question: string;
  context?: string;
  stance: Stance;
  freshSession: boolean;
  model?: string;
  effort?: Effort;
  timeoutMs: number;
  cwd: string;
  skipPermissions: boolean;
  enqueuedAt: number;
  signal?: AbortSignal;
};

type AgyEvent = {
  event?: string;
  conversation_id?: unknown;
  result?: {
    conversation_id?: unknown;
    status?: string;
    response?: string;
    duration_seconds?: number;
    usage?: unknown;
    model?: unknown;
    error?: string;
  };
};

function buildPrompt(question: string, context: string | undefined, stance: Stance, isFollowup: boolean): string {
  const sections: string[] = [];
  if (isFollowup) {
    sections.push(
      "Continue this independent review from the new evidence below. Revise earlier conclusions if the new facts contradict them, and keep separating confirmed facts, inferences, and missing facts.",
    );
  }
  sections.push(question);
  if (context) {
    sections.push(`Known context:\n${context}`);
  }

  if (stance === "red_team") {
    sections.push(
      "Stance (Red-Team Audit): Construct the strongest evidence-based case for why the proposed approach, architecture, or hypothesis will fail to achieve the real end goal. Expose hidden assumptions, proxy-vs-real-goal traps, and single points of failure. Name the cheapest falsifying check and state clearly whether this direction should stop or pivot.",
    );
  } else if (stance === "tactical") {
    sections.push(
      "Stance (Focused Tactical): Answer directly from first principles for the stated scope. Still flag any glaring premise flaw or contradiction in the evidence, distinguish confirmed facts from assumptions, and state concrete pass/stop criteria.",
    );
  } else if (!isFollowup) {
    sections.push(
      "Answer from first principles. Zoom out before zooming in: check whether the question targets a local symptom or sunk-cost patch instead of the real bottleneck. If the question already offers candidate options or a proposed approach, do not simply pick one: derive what you think is right from the goal, the constraints, and the observed failures, separate confirmed facts from inferences and critical unknowns, and state the highest-leverage next step and stop condition.",
    );
  }
  return sections.join("\n\n");
}

export default function (pi: ExtensionAPI) {
  let child: ChildProcessWithoutNullStreams | null = null;
  let startedCwd: string | undefined;
  let startedSkipPermissions: boolean | undefined;
  let startedModel: string | undefined;
  let startedEffort: Effort | undefined;
  let conversationId: string | undefined;
  let pending: { resolve: (v: Advice) => void; reject: (e: Error) => void } | null = null;
  let idle: ReturnType<typeof setTimeout> | null = null;
  let chain: Promise<unknown> = Promise.resolve();
  let stderrTail = "";
  let shuttingDown = false;

  function clearIdle() {
    if (idle) {
      clearTimeout(idle);
      idle = null;
    }
  }

  function scheduleIdle() {
    idle = setTimeout(() => teardown(), IDLE_MS);
    idle.unref();
  }

  function classifyError(message: string, detail?: string): Error {
    const diagnostics = stderrTail ? `\nagy stderr:\n${stderrTail}` : "";
    const combined = `${detail ?? ""} ${stderrTail}`.toLowerCase();
    if (
      combined.includes("sign in") ||
      combined.includes("sign-in") ||
      combined.includes("not logged in") ||
      combined.includes("authenticat")
    ) {
      return new Error(`Antigravity CLI needs an authenticated session. Sign in with \`agy\` on this host.${diagnostics}`);
    }
    if (combined.includes("quota") || combined.includes("resource_exhausted") || combined.includes("rate limit")) {
      return new Error(`Antigravity reported a quota or rate limit.${diagnostics}`);
    }
    return new Error(`${message}${diagnostics}`);
  }

  function teardown(resetConversation = true, error = new Error("agy planner closed")) {
    clearIdle();
    const dying = child;
    child = null;
    startedCwd = undefined;
    startedSkipPermissions = undefined;
    startedModel = undefined;
    startedEffort = undefined;
    stderrTail = "";
    if (resetConversation) conversationId = undefined;

    const request = pending;
    pending = null;
    request?.reject(error);

    if (!dying?.pid || dying.exitCode !== null || dying.signalCode !== null) return;
    const signalChild = (signal: NodeJS.Signals) => {
      try {
        dying.kill(signal);
      } catch {
        // The process may have exited before the signal was sent.
      }
    };
    const forceKill = setTimeout(() => {
      if (dying.exitCode === null && dying.signalCode === null) signalChild("SIGKILL");
    }, KILL_GRACE_MS);
    forceKill.unref();
    dying.once("exit", () => clearTimeout(forceKill));
    signalChild("SIGTERM");
  }

  function start(cwd: string, skipPermissions: boolean, model: string | undefined, effort: Effort | undefined) {
    // NOTE: no --mode flag. Plan mode makes agy write an implementation_plan.md
    // and then wait for a review that headless mode cannot answer, so the turn
    // ends on "please approve" and no advice is ever returned. Leaving the mode
    // unset lets the CLI pick its documented headless default (auto since
    // v1.5.7, falling back to default mode). Passing --mode auto explicitly is
    // wrong here: on accounts where auto is unavailable the CLI prints
    // "--mode auto is not available for this account; continuing in the default
    // mode" on every start.
    const args = ["--input-format", "stream-json", "--output-format", "stream-json"];
    if (skipPermissions) args.push("--dangerously-skip-permissions");
    if (effort) args.push("--effort", effort);
    if (model) args.push("--model", model);
    if (conversationId) args.push("--conversation", conversationId);

    const c = spawn("agy", args, { cwd, stdio: ["pipe", "pipe", "pipe"] });
    child = c;
    startedCwd = cwd;
    startedSkipPermissions = skipPermissions;
    startedModel = model;
    startedEffort = effort;
    let buf = "";
    const fail = (error: Error) => {
      if (child === c) teardown(true, error);
    };

    c.stdout.setEncoding("utf8");
    c.stderr.setEncoding("utf8");
    c.stderr.on("data", (chunk: string) => {
      if (child !== c) return;
      // Keep the tail for diagnostics; an unread stderr pipe would block the child.
      stderrTail = (stderrTail + chunk).slice(-4000);
    });

    c.stdout.on("data", (chunk: string) => {
      if (child !== c) return;
      buf += chunk;
      let nl: number;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        let ev: AgyEvent;
        try {
          ev = JSON.parse(line);
        } catch {
          continue;
        }
        if (!ev || typeof ev !== "object") continue;
        if (ev.event === "init" && typeof ev.conversation_id === "string") conversationId = ev.conversation_id;
        if (ev.event !== "result" || !pending) continue;

        const result = ev.result;
        if (result?.status !== "SUCCESS") {
          fail(classifyError(`agy ${result?.status ?? "error"}: ${result?.error ?? "unknown"}`, result?.error));
          return;
        }
        const response = typeof result.response === "string" ? result.response.trim() : "";
        if (!response) {
          fail(
            classifyError(
              "agy returned an empty response. A tool needed approval that headless mode cannot prompt for. " +
                "If agy's toolPermission is still request-review, set it to always-proceed and keep dangerous commands in a deny list. " +
                "Do not answer this by forbidding agy from using tools. " +
                "Keep the default permissions unless the user explicitly authorizes a bypass.",
            ),
          );
          return;
        }
        const id = result.conversation_id ?? ev.conversation_id;
        if (typeof id === "string") conversationId = id;
        const reportedModel = typeof result.model === "string" ? result.model : undefined;
        const request = pending;
        pending = null;
        scheduleIdle();
        request.resolve({
          response,
          conversationId,
          seconds: result.duration_seconds,
          usage: result.usage,
          model: reportedModel,
        });
      }
    });

    c.stdin.on("error", fail);
    c.stdout.on("error", fail);
    c.stderr.on("error", fail);
    c.on("close", (code, signal) => {
      if (child !== c) return;
      fail(classifyError(`agy planner process exited (code=${code}, signal=${signal})`));
    });
    c.on("error", fail);
  }

  async function ask(opts: AskOptions): Promise<Advice> {
    if (shuttingDown) throw new Error("agy planner closed");
    if (opts.signal?.aborted) throw new Error("cancelled");

    const queueMs = Math.max(0, Date.now() - opts.enqueuedAt);
    const remainingMs = opts.timeoutMs - queueMs;
    if (!Number.isFinite(remainingMs) || remainingMs <= 0) {
      throw new Error(`agy planner timed out after ${opts.timeoutMs}ms waiting for the previous turn`);
    }

    if (opts.model !== undefined && !MODEL_PATTERN.test(opts.model)) {
      throw new Error(`Invalid model identifier: ${opts.model}`);
    }

    // The child can exit before its stdio streams finish closing.
    if (child && (child.exitCode !== null || child.signalCode !== null)) teardown();

    const canReuseProcess = Boolean(child && !opts.freshSession && startedCwd === opts.cwd);
    const envModel = process.env.AGY_ADVISE_MODEL?.trim() || undefined;
    if (!canReuseProcess && opts.model === undefined && envModel && !MODEL_PATTERN.test(envModel)) {
      throw new Error(`Invalid AGY_ADVISE_MODEL identifier: ${envModel}`);
    }

    const effectiveModel = opts.model ?? (canReuseProcess ? startedModel : envModel);
    const effectiveEffort = opts.effort ?? (canReuseProcess ? startedEffort : undefined);

    clearIdle();

    const needsRestart =
      !canReuseProcess ||
      startedSkipPermissions !== opts.skipPermissions ||
      startedModel !== effectiveModel ||
      startedEffort !== effectiveEffort;

    if (needsRestart) {
      const keepConversation =
        canReuseProcess &&
        startedModel === effectiveModel &&
        startedEffort === effectiveEffort;
      teardown(!keepConversation);
      try {
        start(opts.cwd, opts.skipPermissions, effectiveModel, effectiveEffort);
      } catch (error) {
        teardown();
        throw error;
      }
    }

    const isFollowup = Boolean(conversationId);
    const prompt = buildPrompt(opts.question, opts.context, opts.stance, isFollowup);
    const c = child!;
    stderrTail = "";

    return new Promise<Advice>((resolve, reject) => {
      const cleanup = () => {
        clearTimeout(timer);
        opts.signal?.removeEventListener("abort", onAbort);
      };
      const timer = setTimeout(() => {
        teardown(true, new Error(`agy planner timed out after ${opts.timeoutMs}ms`));
      }, remainingMs);
      const onAbort = () => {
        teardown(true, new Error("cancelled"));
      };
      opts.signal?.addEventListener("abort", onAbort, { once: true });

      const request = {
        resolve: (advice: Advice) => {
          cleanup();
          const elapsedMs = Math.max(0, Date.now() - opts.enqueuedAt);
          resolve({
            ...advice,
            queueSeconds: Math.round(queueMs) / 1000,
            elapsedSeconds: Math.round(elapsedMs) / 1000,
          });
        },
        reject: (error: Error) => {
          cleanup();
          reject(error);
        },
      };
      pending = request;
      const payload = JSON.stringify({ event: "user", message: { content: prompt } }) + "\n";
      try {
        c.stdin.write(payload, (error) => {
          if (error && child === c && pending === request) teardown(true, error);
        });
      } catch (error) {
        teardown(true, error instanceof Error ? error : new Error(String(error)));
      }
    });
  }

  pi.on("session_shutdown", async () => {
    shuttingDown = true;
    teardown();
  });

  pi.registerTool({
    name: "agy_advise",
    label: "Antigravity Advisor",
    description:
      "Ask the Antigravity CLI for a plan, design opinion, or independent review. It reads files and runs commands to ground the answer; agy's toolPermission preset decides what it may run without asking. Supports optional stance (global, red_team, tactical), freshSession for clean-room reviews, and model/effort overrides. Returns advice text.",
    promptSnippet: "Ask the Antigravity planner for a plan or independent second opinion",
    promptGuidelines: [
      "Use agy_advise at decision points: choosing an architecture, being stuck for more than one attempt, before a destructive change, or when an independent second review is wanted. Do not call it inside tight tool loops where it only adds latency.",
      "Never pre-collapse the problem for agy_advise. Do not hand it candidate options (A/B/C, \"option 1 or 2\") and do not give it your own conclusion to approve. agy is the strongest model available here: give it the goal, the constraints, and the observed failure, then let it choose the solution space.",
      "Keep agy_advise input factual. Pass the facts agy cannot see (paths, error text, constraints) and one line per hypothesis already ruled out. Do not paste Pi's own reasoning chain, and do not ask agy to re-read files Pi already read.",
      "Use stance=\"red_team\" before high-risk or irreversible changes to stress-test assumptions, and set freshSession=true when switching topics or running a clean-room audit that should not inherit prior conversation bias.",
      "Let agy use read-only tools. It grounds the answer by reading the workspace and running commands; a blocked command means agy's toolPermission preset still asks for approval, which headless mode cannot answer. Raise the preset or deny the dangerous actions instead of forbidding tools.",
      "Never put your own operating limits into the question (\"do not read files\", \"do not run commands\", \"stay static\", \"do not spawn a worker\"). Those constrain Pi, not agy. agy reads them as a narrower task and reports progress instead of answering. Tighten scope through permissions, not through instructions.",
      "Set skipPermissions to true only when the user explicitly authorizes the permission bypass for this request. If a permission problem occurs, explain the blocked operation when the diagnostics identify it.",
    ],
    parameters: Type.Object({
      question: Type.String({ description: "The goal, observed failure if relevant, and open question. Make the first turn self-contained." }),
      context: Type.Optional(Type.String({ description: "Up to 10 lines of relevant facts, paths, constraints or ruled-out hypotheses." })),
      stance: Type.Optional(
        Type.Union([Type.Literal("global"), Type.Literal("red_team"), Type.Literal("tactical")], {
          description: "Review stance: global (default, audit premises before details), red_team (adversarial failure audit), or tactical (focused implementation).",
        }),
      ),
      freshSession: Type.Optional(
        Type.Boolean({
          description: "Start a clean-room agy conversation instead of reusing the active session. Useful when switching topics or auditing without prior bias.",
        }),
      ),
      model: Type.Optional(
        Type.String({
          pattern: MODEL_PATTERN.source,
          description: "Optional Antigravity model override (defaults to AGY_ADVISE_MODEL or CLI default). Changing model starts a fresh session.",
        }),
      ),
      effort: Type.Optional(
        Type.Union(
          [
            Type.Literal("low"),
            Type.Literal("medium"),
            Type.Literal("high"),
            Type.Literal("xhigh"),
            Type.Literal("max"),
          ],
          {
            description: "Optional reasoning effort level. Changing effort starts a fresh session.",
          },
        ),
      ),
      timeoutMs: Type.Optional(Type.Number({ description: "Default 240000" })),
      skipPermissions: Type.Optional(
        Type.Boolean({
          description:
            "Pass --dangerously-skip-permissions, bypassing tool approvals for actions such as shell commands and workspace writes. Requires explicit user authorization.",
        }),
      ),
    }),
    async execute(_id, p, signal, _onUpdate, ctx) {
      const stance: Stance = p.stance ?? "global";
      const freshSession = p.freshSession === true;
      const enqueuedAt = Date.now();

      const run = () =>
        ask({
          question: p.question,
          context: p.context,
          stance,
          freshSession,
          model: p.model,
          effort: p.effort,
          timeoutMs: p.timeoutMs ?? 240_000,
          cwd: ctx.cwd,
          skipPermissions: p.skipPermissions === true,
          enqueuedAt,
          signal,
        });
      const result = chain.then(run, run);
      chain = result.then(
        () => undefined,
        () => undefined,
      );

      const v = await result;
      return {
        content: [{ type: "text", text: v.response }],
        details: {
          conversationId: v.conversationId,
          seconds: v.seconds,
          queueSeconds: v.queueSeconds,
          elapsedSeconds: v.elapsedSeconds,
          model: v.model,
          stance,
          freshSession,
          usage: v.usage,
        },
      };
    },
  });
}
