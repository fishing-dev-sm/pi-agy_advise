"""Lightweight local invocation logging and feedback aggregation for agy_advise."""

import json
import math
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]


FEEDBACK_LABELS = (
    "helpful",
    "rubber_stamp",
    "missed_root_cause",
    "false_alarm",
    "bad_format",
)

_DISABLE_VALUES = {"off", "0", "false", "no", "none"}
_ENABLE_VALUES = {"1", "true", "yes", "on"}
_WS_RE = re.compile(r"\s+")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_data_dir(db_path: Path | None = None) -> Path:
    if db_path is not None:
        return db_path.parent
    if configured := os.getenv("AGY_ADVISE_DB"):
        return Path(configured).expanduser().parent
    default = Path.home() / ".local" / "share"
    return Path(os.getenv("XDG_DATA_HOME", default)) / "codex-agy-advise"


def resolve_log_path(db_path: Path | None = None) -> Path | None:
    configured = os.getenv("AGY_ADVISE_LOG")
    if configured is not None:
        cleaned = configured.strip()
        if cleaned.lower() in _DISABLE_VALUES:
            return None
        if cleaned:
            return Path(cleaned).expanduser()
    return default_data_dir(db_path) / "invocations.jsonl"


def resolve_feedback_path(db_path: Path | None = None) -> Path:
    if configured := os.getenv("AGY_ADVISE_FEEDBACK_LOG"):
        cleaned = configured.strip()
        if cleaned:
            return Path(cleaned).expanduser()
    log_path = resolve_log_path(db_path)
    if log_path is not None:
        return log_path.parent / "feedback.jsonl"
    return default_data_dir(db_path) / "feedback.jsonl"


def log_full_enabled() -> bool:
    return os.getenv("AGY_ADVISE_LOG_FULL", "").strip().lower() in _ENABLE_VALUES


def preview_text(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    compact = _WS_RE.sub(" ", text).strip()
    if not compact:
        return None
    if len(compact) <= limit:
        return compact
    return compact[: max(limit - 1, 1)].rstrip() + "…"


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    line = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            view = memoryview(line)
            written = 0
            while written < len(view):
                written += os.write(fd, view[written:])
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def build_invocation_record(
    *,
    invocation_id: str,
    mode: str,
    action: str,
    status: str,
    elapsed_seconds: float,
    question: str = "",
    context: str | None = None,
    stance: str | None = None,
    is_followup: bool = False,
    session_id: str | None = None,
    effort: str | None = None,
    model_requested: str | None = None,
    project_id: str | None = None,
    phase: str | None = None,
    response: dict[str, Any] | None = None,
    error: str | None = None,
    include_full: bool | None = None,
) -> dict[str, Any]:
    resp = response or {}
    proj = resp.get("project") if isinstance(resp.get("project"), dict) else {}
    last_decision = proj.get("last_decision") if isinstance(proj.get("last_decision"), dict) else {}
    advice = resp.get("advice") if isinstance(resp.get("advice"), str) else None
    usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else None
    review_status = resp.get("review_status") if isinstance(resp.get("review_status"), str) else None

    effective_model_req = resp.get("model_requested") if resp.get("model_requested") is not None else model_requested
    effective_effort = resp.get("requested_effort") if resp.get("requested_effort") is not None else effort
    effective_session = resp.get("session_id") if isinstance(resp.get("session_id"), str) else session_id

    llm_called = bool(
        (mode == "qa" and status != "validation_error")
        or (
            mode == "project"
            and review_status in ("decided", "stale", "transport_error", "invalid_decision")
        )
    )

    decision_value = None
    if review_status == "decided" and isinstance(last_decision.get("decision"), str):
        decision_value = last_decision["decision"]

    record: dict[str, Any] = {
        "ts": utc_now(),
        "invocation_id": invocation_id,
        "mode": mode,
        "action": action,
        "status": status,
        "llm_called": llm_called,
        "elapsed_seconds": round(max(elapsed_seconds, 0.0), 3),
        "queue_seconds": resp.get("queue_seconds"),
        "duration_seconds": resp.get("duration_seconds"),
        "model_requested": effective_model_req,
        "model_reported": resp.get("model_reported"),
        "requested_effort": effective_effort,
        "usage": usage,
        "stance": resp.get("requested_stance", stance) if mode == "qa" else None,
        "is_followup": bool(is_followup) if mode == "qa" else False,
        "session_id": effective_session if mode == "qa" else None,
        "project_id": proj.get("id", project_id),
        "phase": proj.get("phase", phase),
        "domain": proj.get("domain"),
        "gate_mode": proj.get("gate_mode"),
        "route": proj.get("route"),
        "cycle": proj.get("cycle"),
        "review_status": review_status,
        "review_reasons": proj.get("review_reasons") if isinstance(proj.get("review_reasons"), list) else [],
        "decision": decision_value,
        "dispatch_allowed": proj.get("dispatch_allowed"),
        "question_chars": len(question or ""),
        "context_chars": len(context or ""),
        "advice_chars": len(advice) if advice is not None else 0,
        "question_preview": preview_text(question, 240),
        "advice_preview": preview_text(advice, 320),
        "error": preview_text(error or (resp.get("error") if isinstance(resp.get("error"), str) else None), 400),
    }

    full = log_full_enabled() if include_full is None else include_full
    if full:
        record["question"] = question
        record["context"] = context
        record["advice"] = advice
    return record


def record_invocation(db_path: Path | None, **kwargs: Any) -> Path | None:
    log_path = resolve_log_path(db_path)
    if log_path is None:
        return None
    record = build_invocation_record(**kwargs)
    try:
        append_jsonl(log_path, record)
    except OSError:
        return None
    return log_path


def record_feedback(
    *,
    invocation_id: str,
    label: str,
    note: str = "",
    project_id: str | None = None,
    phase: str | None = None,
    db_path: Path | None = None,
    feedback_path: Path | None = None,
) -> Path:
    if label not in FEEDBACK_LABELS:
        raise ValueError(f"Unsupported feedback label {label!r}; choose one of {', '.join(FEEDBACK_LABELS)}.")
    if not invocation_id.strip():
        raise ValueError("invocation_id must not be empty.")
    target = feedback_path or resolve_feedback_path(db_path)
    entry = {
        "ts": utc_now(),
        "invocation_id": invocation_id.strip(),
        "label": label,
        "note": note.strip(),
        "project_id": project_id,
        "phase": phase,
    }
    append_jsonl(target, entry)
    return target


def read_jsonl(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
    return rows


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return round(ordered[index], 3)


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def summarize_logs(
    log_path: Path | None,
    feedback_path: Path | None = None,
    *,
    project_id: str | None = None,
    days: int | None = None,
    recent: int = 8,
) -> dict[str, Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days is not None else None
    invocations = read_jsonl(log_path)
    feedback_rows = read_jsonl(feedback_path)

    filtered: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for row in invocations:
        if project_id is not None and row.get("project_id") != project_id:
            continue
        if cutoff is not None:
            ts = _parse_ts(row.get("ts"))
            if ts is not None and ts < cutoff:
                continue
        filtered.append(row)
        inv_id = row.get("invocation_id")
        if isinstance(inv_id, str):
            by_id[inv_id] = row

    filtered_feedback: list[dict[str, Any]] = []
    for fb in feedback_rows:
        inv_id = fb.get("invocation_id")
        linked = by_id.get(inv_id) if isinstance(inv_id, str) else None
        effective_project = fb.get("project_id") or (linked.get("project_id") if linked else None)
        if project_id is not None and effective_project != project_id:
            continue
        if cutoff is not None:
            ts = _parse_ts(fb.get("ts"))
            if ts is not None and ts < cutoff:
                continue
        filtered_feedback.append({**fb, "linked": linked})

    mode_counts = Counter(row.get("mode", "unknown") for row in filtered)
    action_counts = Counter(f"{row.get('mode', '?')}:{row.get('action', '?')}" for row in filtered)
    status_counts = Counter(row.get("status", "unknown") for row in filtered)
    stance_counts = Counter(row["stance"] for row in filtered if row.get("mode") == "qa" and row.get("stance"))
    model_counts = Counter(
        row.get("model_reported") or row.get("model_requested") or "default"
        for row in filtered
        if row.get("llm_called")
    )
    project_counts = Counter(row["project_id"] for row in filtered if row.get("project_id"))
    review_status_counts = Counter(
        row["review_status"] for row in filtered if row.get("mode") == "project" and row.get("review_status")
    )
    trigger_counts: Counter[str] = Counter()
    for row in filtered:
        if row.get("mode") != "project":
            continue
        reasons = row.get("review_reasons") or []
        if row.get("action") == "report":
            for reason in reasons:
                if reason != "explicit_review":
                    trigger_counts[reason] += 1
        elif row.get("action") == "review" and "explicit_review" in reasons:
            trigger_counts["explicit_review"] += 1
    decision_counts = Counter(row["decision"] for row in filtered if row.get("decision"))

    llm_rows = [row for row in filtered if row.get("llm_called")]
    llm_elapsed = [float(row["elapsed_seconds"]) for row in llm_rows if isinstance(row.get("elapsed_seconds"), (int, float))]
    llm_queue = [float(row["queue_seconds"]) for row in llm_rows if isinstance(row.get("queue_seconds"), (int, float))]
    total_tokens = 0
    for row in llm_rows:
        usage = row.get("usage")
        if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), (int, float)):
            total_tokens += int(usage["total_tokens"])

    qa_rows = [row for row in filtered if row.get("mode") == "qa"]
    followup_count = sum(1 for row in qa_rows if row.get("is_followup"))
    timeout_count = sum(1 for row in llm_rows if row.get("status") == "timeout" or row.get("review_status") == "transport_error")

    feedback_counts = Counter(fb.get("label", "unknown") for fb in filtered_feedback)
    feedback_by_stance: dict[str, dict[str, int]] = {}
    for fb in filtered_feedback:
        linked = fb.get("linked")
        key = (linked.get("stance") or linked.get("mode") or "unlinked") if linked else "unlinked"
        bucket = feedback_by_stance.setdefault(key, {})
        label = fb.get("label", "unknown")
        bucket[label] = bucket.get(label, 0) + 1

    hints: list[str] = []
    if llm_rows and timeout_count / len(llm_rows) >= 0.15:
        hints.append(
            f"High timeout/transport error rate ({timeout_count}/{len(llm_rows)}): "
            "consider shorter focused questions, lower effort, or auto_review=false batching."
        )
    review_attempts = sum(
        review_status_counts[k] for k in ("decided", "stale", "transport_error", "invalid_decision")
    )
    if review_attempts and review_status_counts["invalid_decision"] / review_attempts >= 0.1:
        hints.append(
            f"Elevated invalid_decision rate ({review_status_counts['invalid_decision']}/{review_attempts}): "
            "inspect advice_preview and tighten review_prompt / decode_decision."
        )
    if review_attempts and review_status_counts["stale"] / review_attempts >= 0.15:
        hints.append(
            f"Frequent stale reviews ({review_status_counts['stale']}/{review_attempts}): "
            "use auto_review=false when parallel trials finish together, then run a single review."
        )
    if feedback_counts["rubber_stamp"] > 0:
        hints.append(
            f"{feedback_counts['rubber_stamp']} rubber_stamp feedback item(s): "
            "audit question framing or default to stance='red_team' for high-stakes reviews."
        )
    if feedback_counts["false_alarm"] > 0:
        hints.append(
            f"{feedback_counts['false_alarm']} false_alarm feedback item(s): "
            "review stagnation_limit / same_mechanism_limit / proxy_limit thresholds."
        )

    recent_entries = [
        {
            "ts": row.get("ts"),
            "invocation_id": row.get("invocation_id"),
            "mode": row.get("mode"),
            "action": row.get("action"),
            "status": row.get("status"),
            "project_id": row.get("project_id"),
            "phase": row.get("phase"),
            "stance": row.get("stance"),
            "review_status": row.get("review_status"),
            "decision": row.get("decision"),
            "elapsed_seconds": row.get("elapsed_seconds"),
            "question_preview": row.get("question_preview"),
            "error": row.get("error"),
        }
        for row in filtered[-recent:]
    ]

    return {
        "log_path": str(log_path) if log_path else None,
        "feedback_path": str(feedback_path) if feedback_path else None,
        "filters": {"project_id": project_id, "days": days},
        "total_invocations": len(filtered),
        "llm_invocations": len(llm_rows),
        "mode_counts": dict(mode_counts),
        "action_counts": dict(action_counts),
        "status_counts": dict(status_counts),
        "timeout_or_transport_errors": timeout_count,
        "timeout_rate": round(timeout_count / len(llm_rows), 3) if llm_rows else 0.0,
        "latency_seconds": {
            "p50": percentile(llm_elapsed, 50),
            "p95": percentile(llm_elapsed, 95),
            "max": round(max(llm_elapsed), 3) if llm_elapsed else None,
            "queue_p95": percentile(llm_queue, 95),
        },
        "total_tokens": total_tokens,
        "qa": {
            "total": len(qa_rows),
            "followups": followup_count,
            "stance_counts": dict(stance_counts),
        },
        "models": dict(model_counts),
        "projects": dict(project_counts),
        "review_status_counts": dict(review_status_counts),
        "trigger_counts": dict(trigger_counts),
        "decision_counts": dict(decision_counts),
        "feedback": {
            "total": len(filtered_feedback),
            "label_counts": dict(feedback_counts),
            "by_context": feedback_by_stance,
        },
        "tuning_hints": hints,
        "recent": recent_entries,
    }


def format_summary(summary: dict[str, Any]) -> str:
    lines = [
        "# agy_advise Invocation & Feedback Report",
        "",
        f"- **Log**: `{summary['log_path']}`",
        f"- **Total Invocations**: {summary['total_invocations']} (LLM calls: {summary['llm_invocations']})",
        f"- **Modes**: {summary['mode_counts'] or '{}'}",
        f"- **Statuses**: {summary['status_counts'] or '{}'} (timeout/transport rate: {summary['timeout_rate']:.1%})",
    ]
    lat = summary["latency_seconds"]
    if lat["p50"] is not None:
        lines.append(
            f"- **LLM Latency**: P50={lat['p50']}s, P95={lat['p95']}s, max={lat['max']}s "
            f"(queue P95={lat['queue_p95']}s) | **Total Tokens**: {summary['total_tokens']}"
        )
    if summary["models"]:
        lines.append(f"- **Models**: {summary['models']}")
    if summary["qa"]["total"]:
        lines.append(
            f"- **Q&A**: total={summary['qa']['total']}, followups={summary['qa']['followups']}, "
            f"stances={summary['qa']['stance_counts']}"
        )
    if summary["projects"]:
        lines.append(f"- **Projects**: {summary['projects']}")
    if summary["review_status_counts"]:
        lines.append(f"- **Project Review Statuses**: {summary['review_status_counts']}")
    if summary["trigger_counts"]:
        lines.append(f"- **Review Triggers**: {summary['trigger_counts']}")
    if summary["decision_counts"]:
        lines.append(f"- **Decisions**: {summary['decision_counts']}")
    if summary["feedback"]["total"]:
        lines.append(
            f"- **Feedback ({summary['feedback']['total']})**: {summary['feedback']['label_counts']} "
            f"| by context: {summary['feedback']['by_context']}"
        )
    if summary["tuning_hints"]:
        lines.extend(["", "## Tuning Hints"])
        for hint in summary["tuning_hints"]:
            lines.append(f"- {hint}")
    if summary["recent"]:
        lines.extend(["", "## Recent Calls"])
        for item in summary["recent"]:
            target = f"{item['project_id']}/{item['phase']}" if item.get("project_id") else (item.get("stance") or "qa")
            extra = item.get("decision") or item.get("review_status") or item.get("error") or ""
            extra_part = f" [{extra}]" if extra else ""
            lines.append(
                f"- `{item['invocation_id'][:10]}` {item['ts']} {item['mode']}:{item['action']} "
                f"({target}) {item['status']} {item['elapsed_seconds']}s{extra_part}"
            )
    return "\n".join(lines)
