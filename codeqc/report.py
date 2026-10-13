from __future__ import annotations

import json
import html as html_module
import re
from datetime import datetime, timezone
from pathlib import Path

import bleach
import markdown
from jinja2 import Environment, PackageLoader, select_autoescape


ALLOWED_TAGS = set(bleach.sanitizer.ALLOWED_TAGS) | {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "pre", "code", "table", "thead",
    "tbody", "tr", "th", "td", "hr", "br", "details", "summary",
}

TEMPLATES = Environment(
    loader=PackageLoader("codeqc", "templates"),
    autoescape=select_autoescape(("html", "xml")),
)

NO_RESPONSE_MESSAGE = "No response"


def render_template(name: str, **context) -> str:
    return TEMPLATES.get_template(name).render(**context)


def write_report(report_dir: Path, results_id: str, content: str, metadata: dict) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    content = content.strip() or NO_RESPONSE_MESSAGE
    body = bleach.clean(
        markdown.markdown(content, extensions=["fenced_code", "tables", "sane_lists"]),
        tags=ALLOWED_TAGS,
        attributes={"a": ["href", "title"]},
        protocols={"http", "https", "mailto"},
    )
    generated = datetime.now(timezone.utc).isoformat()
    files_html = render_file_list(metadata.get("files") or [])
    trace_html = render_trace(metadata.get("trace") or [])
    elapsed = format_elapsed(metadata)
    finished = str(metadata.get("finished_at") or "—")
    actions = render_report_actions(
        results_id,
        active=False,
        status="completed",
    )
    html = render_template(
        "completed_report.html", results_id=results_id,
        model_name=metadata.get("model_name", ""), review_type=metadata.get("type", ""),
        generated=generated, elapsed=elapsed, finished=finished, actions=actions,
        user_prompt=str(metadata.get("user_prompt") or ""),
        files_html=files_html, trace_html=trace_html, body=body,
        file_list_script=file_list_state_script(results_id),
        checklist_script=checklist_script(results_id),
    )
    (report_dir / f"{results_id}.html").write_text(html, encoding="utf-8")
    (report_dir / f"{results_id}.md").write_text(
        render_completed_markdown(content, results_id, metadata), encoding="utf-8"
    )
    (report_dir / f"{results_id}.json").write_text(
        json.dumps({**metadata, "results_id": results_id, "generated_at": generated}, indent=2), encoding="utf-8"
    )


def render_job_page(job: dict) -> str:
    status = str(job.get("status", "unknown"))
    progress = str(job.get("progress", ""))
    exception = str(job.get("exception", ""))
    files_html = render_file_list(job.get("files") or [])
    trace_html = render_trace(job.get("trace") or [], element_id="trace-details")
    results_id = str(job.get("results_id", ""))
    elapsed = format_elapsed(job)
    finished = str(job.get("finished_at") or "—")
    actions = render_report_actions(
        results_id,
        status in {"queued", "running"},
        status=status,
    )
    return render_template(
        "job_report.html", status=status, status_html=render_status(status), progress=progress,
        exception=exception, files_html=files_html, trace_html=trace_html,
        user_prompt=str(job.get("user_prompt") or ""),
        results_id=results_id, elapsed=elapsed, finished=finished, actions=actions,
        polling_script=job_polling_script(results_id, status),
    )


def render_report_actions(
    results_id: str,
    active: bool,
    status: str | None = None,
) -> str:
    return render_template(
        "partials/report_actions.html", results_id=results_id, active=active,
        completed=status == "completed",
    )


def render_job_markdown(job: dict) -> str:
    status = str(job.get("status", "unknown"))
    progress = str(job.get("progress", ""))
    files = job.get("files") or []
    file_section = "\n".join(f"- `{item}`" for item in files) or "- No files recorded."
    exception = str(job.get("exception", ""))
    exception_section = f"\n\n## Exception\n\n```text\n{exception}\n```" if exception else ""
    user_prompt = str(job.get("user_prompt") or "")
    prompt_section = f"\n\n## User prompt\n\n{user_prompt}" if user_prompt else ""
    trace = job.get("trace") or []
    trace_section = "\n".join(
        f"- `{item.get('time', '')}` {item.get('message', '')}" for item in trace if isinstance(item, dict)
    ) or "- LangGraph has not started yet."
    return (
        f"# AI CICD Security Guardrail report\n\n**Status:** {status}\n\n{progress}\n\n"
        f"**Elapsed time:** {format_elapsed(job)}  \n"
        f"**Finished time:** {job.get('finished_at') or '—'}\n\n"
        f"## Files ({len(files)})\n\n{file_section}\n\n"
        f"## LangGraph trace ({len(trace)})\n\n{trace_section}\n\n"
        f"Result: `{job.get('results_id', '')}`{prompt_section}{exception_section}\n"
    )


def render_completed_markdown(content: str, results_id: str, metadata: dict) -> str:
    content = content.strip() or NO_RESPONSE_MESSAGE
    files = metadata.get("files") or []
    trace = metadata.get("trace") or []
    file_section = "\n".join(f"- `{item}`" for item in files) or "- No files recorded."
    trace_section = "\n".join(
        f"- `{item.get('time', '')}` {item.get('message', '')}" for item in trace if isinstance(item, dict)
    ) or "- No LangGraph trace entries recorded."
    user_prompt = str(metadata.get("user_prompt") or "")
    prompt_section = f"## User prompt\n\n{user_prompt}\n\n" if user_prompt else ""
    return (
        f"# AI CICD Security Guardrail report\n\n"
        f"- Result: `{results_id}`\n"
        f"- Model: `{metadata.get('model_name', '')}`\n"
        f"- Review type: `{metadata.get('type', '')}`\n"
        f"- Elapsed time: {format_elapsed(metadata)}\n"
        f"- Finished time: {metadata.get('finished_at') or '—'}\n\n"
        f"## Files ({len(files)})\n\n{file_section}\n\n"
        f"## LangGraph trace ({len(trace)})\n\n{trace_section}\n\n"
        f"{prompt_section}"
        f"## Review\n\n{content}\n"
    )


def markdown_to_plain(content: str) -> str:
    rendered = markdown.markdown(content, extensions=["fenced_code", "tables", "sane_lists"])
    rendered = re.sub(
        r"</?(?:h[1-6]|p|div|pre|table|thead|tbody|tr|ul|ol|blockquote|hr)[^>]*>",
        "\n",
        rendered,
    )
    rendered = re.sub(r"</?(?:li|th|td)[^>]*>", "\n", rendered)
    text = bleach.clean(rendered, tags=set(), strip=True)
    text = html_module.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def render_reports_page(jobs: list[dict]) -> str:
    rendered_jobs: list[dict] = []
    for job in jobs:
        raw_status = str(job.get("status", "unknown"))
        rendered_jobs.append({
            "results_id": str(job.get("results_id", "")), "status": raw_status,
            "status_html": render_status(raw_status), "model_name": str(job.get("model_name", "")),
            "review_type": str(job.get("type", "")), "created_at": str(job.get("created_at", "")),
            "elapsed": format_elapsed(job),
            "active": raw_status in {"queued", "running"},
            "completed": raw_status == "completed",
        })
    return render_template("reports.html", jobs=rendered_jobs)


def render_file_list(files: list[str]) -> str:
    return render_template("partials/file_list.html", files=[str(path) for path in files])


def render_status(status: str) -> str:
    return render_template("partials/status.html", status=status)


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def format_elapsed(job: dict) -> str:
    started = _parse_time(job.get("started_at") or job.get("created_at"))
    if started is None:
        return "—"
    finished = _parse_time(job.get("finished_at")) or datetime.now(timezone.utc)
    seconds = max(0, int((finished - started).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def render_trace(trace: list[dict], element_id: str = "") -> str:
    return render_template("partials/trace.html", trace=trace, element_id=element_id)


def file_list_state_script(results_id: str) -> str:
    return render_template("scripts/file_list_state.html", results_id=results_id)


def checklist_script(results_id: str) -> str:
    return render_template("scripts/checklist.html", results_id=results_id)


def enhance_existing_report_html(content: str, results_id: str, status: str | None = None) -> str:
    """Add checklist support when serving reports generated by an older release."""
    if status is not None:
        current_actions = render_report_actions(
            results_id, active=status in {"queued", "running"}, status=status
        )
        content, replacements = re.subn(
            r'<nav class="report-actions">.*?</nav>',
            lambda _: current_actions,
            content,
            count=1,
            flags=re.DOTALL,
        )
        if replacements == 0:
            content = content.replace("<body>", "<body>" + current_actions, 1)

    seen_actions = False

    def keep_first_actions(match: re.Match) -> str:
        nonlocal seen_actions
        if seen_actions:
            return ""
        seen_actions = True
        return match.group(0)

    content = re.sub(
        r'<nav class="report-actions">.*?</nav>',
        keep_first_actions,
        content,
        flags=re.DOTALL,
    )
    if "data-codeqc-report-layout" not in content:
        content = content.replace("</head>", render_template("partials/legacy_layout_style.html") + "</head>")
    if 'data-codeqc-checklist="2"' in content:
        return content
    if "data-codeqc-checklist" in content:
        upgraded = re.sub(
            r"<script data-codeqc-checklist(?:=\"[^\"]*\")?>.*?</script>",
            lambda _: checklist_script(results_id),
            content,
            count=1,
            flags=re.DOTALL,
        )
        return upgraded.replace("</head>", render_template("partials/legacy_checklist_upgrade_style.html") + "</head>")
    controls = render_template("partials/legacy_checklist_controls.html")
    content = content.replace("</nav>", controls + "</nav>")
    styles = render_template("partials/legacy_checklist_styles.html")
    return content.replace("</head>", styles + "</head>").replace(
        "</body>", checklist_script(results_id) + "</body>"
    )


def job_polling_script(results_id: str, initial_status: str) -> str:
    return render_template(
        "scripts/job_polling.html", results_id=results_id, initial_status=initial_status
    )
