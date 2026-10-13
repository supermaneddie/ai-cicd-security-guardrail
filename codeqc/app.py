from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, redirect, render_template, request, send_file
from redis import Redis
from rq import Queue
from rq.command import send_stop_job_command
from rq.exceptions import NoSuchJobError
from rq.exceptions import InvalidJobOperation
from rq.job import Job as RQJob
from werkzeug.exceptions import RequestEntityTooLarge

from .agent import TYPE_GUIDANCE
from .config import PublicURLConfig, load_config
from .redis_jobs import RedisJobStore
from .status import report_status_payload
from .report import enhance_existing_report_html, markdown_to_plain, render_job_markdown, render_job_page, render_reports_page
from .tasks import execute_review_task, review_job_failed, review_job_stopped
from .workspace import list_files, save_uploads


LOG = logging.getLogger(__name__)
MAX_USER_PROMPT_LENGTH = 10_000


def _gitlab_ci_template_path() -> Path:
    return Path(__file__).resolve().parents[1] / "scripts" / ".gitlab-ci.yml"


def _default_listening_files(template: str) -> list[str]:
    """Return the change patterns from the template's listening-files rule."""
    lines = template.splitlines()
    files: list[str] = []
    in_changes = False
    for line in lines:
        stripped = line.strip()
        if re.fullmatch(r"-\s+changes:\s+&MONITOR_FILES", stripped):
            in_changes = True
            continue
        if in_changes:
            match = re.fullmatch(r'-\s+["\']?(.*?)["\']?', stripped)
            if match:
                files.append(match.group(1))
                continue
            if stripped:
                break
    return files


def _generate_gitlab_ci(
    template: str,
    listening_files: bool,
    files: list[str],
    web_pipeline: bool,
    public_url: PublicURLConfig | None = None,
) -> str:
    """Render deployment settings and the selected pipeline rules."""
    public_url = public_url or PublicURLConfig()
    replacements = {
        "__CODE_QC_URL__": public_url.http_url,
        "__CODE_QC_WS_URL__": public_url.websocket_url,
        "__DOCKER_IMAGE__": public_url.docker_image,
    }
    template = re.sub(
        "|".join(replacements),
        lambda match: json.dumps(replacements[match.group(0)]),
        template,
    )
    template = re.sub(
        r"(?ms)^\.monitor_files:\n.*?(?=^code_qc:\n)",
        "",
        template,
        count=1,
    )
    prefix, separator, _rules = template.partition("  rules:\n")
    if not separator:
        raise ValueError("GitLab CI template does not contain a rules section")

    rules: list[str] = []
    if listening_files:
        cleaned_files = [item.strip() for item in files if item.strip()]
        if not cleaned_files:
            raise ValueError("Listening files requires at least one file pattern")
        monitor_files = [
            ".monitor_files:",
            "  rules:",
            "    - changes: &MONITOR_FILES",
            *[f"        - {json.dumps(item)}" for item in cleaned_files],
            "",
        ]
        prefix = prefix.replace(
            "code_qc:\n",
            "\n".join(monitor_files) + "\ncode_qc:\n",
            1,
        )
        rules.extend([
            '    - if: $CI_PIPELINE_SOURCE != "merge_request_event"',
            "      changes: *MONITOR_FILES",
            "      variables:",
            "        MONITOR_FILES: |",
            *[f"          {item}" for item in cleaned_files],
            "      when: manual",
        ])
    if web_pipeline:
        rules.extend([
            '    - if: $CI_PIPELINE_SOURCE == "web"',
            "      when: manual",
        ])
    if not rules:
        raise ValueError("Select at least one pipeline rule")
    return prefix + "  rules:\n" + "\n".join(rules) + "\n"


def _combined_user_prompt(form_prompt: str, prompt_uploads: list) -> str:
    parts = []
    if form_prompt.strip():
        parts.append(form_prompt.strip())
    for upload in prompt_uploads:
        if not upload.filename:
            continue
        try:
            prompt = upload.read().decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ValueError("prompt file must be UTF-8 text") from exc
        if prompt:
            parts.append(prompt)
    return "\n\n".join(parts)


def create_app() -> Flask:
    app = Flask(__name__)
    environ = __import__("os").environ
    config_path = Path(app.config.get("CODEQC_CONFIG") or environ.get("CODEQC_CONFIG", "config.yaml"))
    settings = load_config(config_path)
    report_dir = Path(environ.get("CODEQC_REPORT_DIR", "data/reports")).resolve()
    job_dir = Path(environ.get("CODEQC_JOB_DIR", "data/jobs")).resolve()
    workspace_dir = job_dir / "workspaces"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    redis_url = environ.get("REDIS_URL", "redis://localhost:6379/0")
    redis_prefix = environ.get("CODEQC_REDIS_PREFIX", "codeqc")
    queue_name = environ.get("CODEQC_QUEUE", "codeqc")
    job_timeout = int(environ.get("CODEQC_JOB_TIMEOUT", "3600"))
    websocket_port = int(environ.get("CODEQC_WS_PORT", "8081"))
    websocket_public_url = environ.get("CODEQC_WS_PUBLIC_URL", "").rstrip("/")
    state_redis = Redis.from_url(redis_url, decode_responses=True)
    state_redis.ping()
    rq_redis = Redis.from_url(redis_url)
    jobs = RedisJobStore(state_redis, redis_prefix)
    imported_jobs = jobs.import_json_directory(job_dir)
    if imported_jobs:
        LOG.info("Imported %d legacy AI CICD Security Guardrail job(s) into Redis", imported_jobs)
    cleaned_workspaces = 0
    for stored_job in jobs.list_all():
        if stored_job.get("status") in {"queued", "running"}:
            continue
        stored_id = str(stored_job.get("results_id", ""))
        if len(stored_id) != 32:
            continue
        retained = workspace_dir / stored_id
        if retained.is_dir():
            shutil.rmtree(retained, ignore_errors=True)
            cleaned_workspaces += 1
        if stored_job.get("workspace_available") is not False:
            jobs.update(stored_id, workspace_available=False)
    if cleaned_workspaces:
        LOG.info("Removed %d workspace(s) belonging to terminated jobs", cleaned_workspaces)
    review_queue = Queue(queue_name, connection=rq_redis)
    app.extensions["codeqc_jobs"] = jobs
    app.extensions["codeqc_queue"] = review_queue
    app.config["MAX_CONTENT_LENGTH"] = settings.limits.max_upload_mb * 1024 * 1024

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        return jsonify(error="Request exceeds the configured upload limit"), 413

    @app.get("/")
    def index():
        return redirect("/reports", code=302)

    @app.get("/health")
    def health():
        return jsonify(status="ok", models=sorted(settings.models))

    @app.get("/models")
    def models():
        return jsonify(models=sorted(settings.models))

    @app.get("/qc.sh")
    def download_qc_script():
        script_path = Path(__file__).resolve().parents[1] / "scripts" / "qc.sh"
        return send_file(
            script_path,
            mimetype="text/x-shellscript",
            as_attachment=True,
            download_name="qc.sh",
        )

    @app.route("/generate-cicd", methods=["GET", "POST"])
    def generate_cicd():
        template = _gitlab_ci_template_path().read_text(encoding="utf-8")
        defaults = _default_listening_files(template)
        if request.method == "GET":
            return render_template(
                "generate_cicd.html",
                listening_files=True,
                web_pipeline=True,
                files="\n".join(defaults),
                error="",
            )

        listening_files = request.form.get("listening_files") == "on"
        web_pipeline = request.form.get("web_pipeline") == "on"
        raw_files = request.form.get("files", "")
        try:
            generated = _generate_gitlab_ci(
                template,
                listening_files,
                raw_files.splitlines(),
                web_pipeline,
                settings.public_url,
            )
        except ValueError as exc:
            return render_template(
                "generate_cicd.html",
                listening_files=listening_files,
                web_pipeline=web_pipeline,
                files=raw_files,
                error=str(exc),
            ), 400
        return generated, 200, {
            "Content-Type": "text/plain; charset=utf-8",
        }

    @app.get("/reports")
    def reports():
        report_jobs = [_sync_rq_status(job) for job in jobs.list_all()]
        return render_reports_page(report_jobs), 200, {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
        }

    def enqueue_review(
        results_id: str,
        workspace: Path,
        model_name: str,
        review_type: str,
        user_prompt: str,
        files: list[str],
    ) -> None:
        review_queue.enqueue(
            execute_review_task,
            args=(
                results_id,
                str(config_path.resolve()),
                redis_url,
                redis_prefix,
                str(report_dir),
                str(workspace),
                model_name,
                review_type,
                user_prompt,
                files,
            ),
            job_id=results_id,
            job_timeout=job_timeout,
            result_ttl=-1,
            failure_ttl=-1,
            on_failure=review_job_failed,
            on_stopped=review_job_stopped,
        )

    def _sync_rq_status(job: dict) -> dict:
        if job.get("status") not in {"queued", "running"}:
            return job
        results_id = str(job.get("results_id", ""))
        try:
            rq_job = RQJob.fetch(results_id, connection=rq_redis)
            rq_status = rq_job.get_status(refresh=True)
            rq_status = getattr(rq_status, "value", str(rq_status))
        except NoSuchJobError:
            rq_status = "missing"
            rq_job = None
        if rq_status in {"stopped", "canceled"}:
            shutil.rmtree(workspace_dir / results_id, ignore_errors=True)
            return jobs.update(
                results_id,
                status="stopped",
                finished_at=datetime.now(timezone.utc).isoformat(),
                progress="Review stopped before completion.",
                exception="",
                workspace_available=False,
            )
        if rq_status in {"failed", "missing", "finished"}:
            exception = getattr(rq_job, "exc_info", None) if rq_job else None
            detail = (
                "RQ finished, but AI CICD Security Guardrail report state was not finalized."
                if rq_status == "finished"
                else f"Background job status is {rq_status}."
            )
            shutil.rmtree(workspace_dir / results_id, ignore_errors=True)
            return jobs.update(
                results_id,
                status="failed",
                finished_at=datetime.now(timezone.utc).isoformat(),
                progress=f"RQ job is {rq_status}; the review is no longer running.",
                exception=exception or f"RQJobError: {detail}",
                workspace_available=False,
            )
        return job

    @app.post("/api/codeqc")
    async def codeqc():
        uploads = request.files.getlist("files") or request.files.getlist("file")
        default_model = next(iter(settings.models))
        model_name = (
            request.form.get("model_name")
            or request.args.get("model_name")
            or request.form.get("model")
            or request.args.get("model")
            or default_model
        ).strip() or default_model
        review_type = (
            request.form.get("review_type")
            or request.args.get("review_type")
            or request.form.get("type")
            or request.args.get("type")
            or "all"
        ).strip().lower() or "all"
        try:
            user_prompt = _combined_user_prompt(
                request.form.get("user_prompt") or "",
                request.files.getlist("prompt"),
            )
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        if not uploads or not any(upload.filename for upload in uploads):
            return jsonify(error="At least one file is required in multipart field 'files'"), 400
        if model_name not in settings.models:
            return jsonify(error="Unknown model_name", available_models=sorted(settings.models)), 400
        if review_type not in TYPE_GUIDANCE:
            return jsonify(error="type must be syntax, suggestion, enhancement, or all"), 400
        if len(user_prompt) > MAX_USER_PROMPT_LENGTH:
            return jsonify(error="user_prompt exceeds 10000 characters"), 400

        temp_dir = Path(tempfile.mkdtemp(prefix="codeqc-", dir=str(job_dir)))
        retained_workspace = None
        queued = False
        try:
            workspace = temp_dir / "workspace"
            save_uploads(uploads, workspace, settings.limits)
            files = list_files(workspace)
            if not files:
                return jsonify(error="No reviewable files found"), 400
            results_id = uuid.uuid4().hex
            retained_workspace = workspace_dir / results_id
            shutil.move(str(workspace), retained_workspace)
            jobs.create(
                results_id,
                model_name,
                review_type,
                files,
                user_prompt=user_prompt,
            )
            enqueue_review(
                results_id,
                retained_workspace,
                model_name,
                review_type,
                user_prompt,
                files,
            )
            queued = True
            if websocket_public_url:
                websocket_url = websocket_public_url
            else:
                hostname = urlsplit(f"//{request.host}").hostname or "localhost"
                if ":" in hostname:
                    hostname = f"[{hostname}]"
                websocket_scheme = "wss" if request.scheme == "https" else "ws"
                websocket_url = f"{websocket_scheme}://{hostname}:{websocket_port}"
            return jsonify(
                results_id=results_id,
                status="queued",
                report_url=f"/report?results_id={results_id}",
                websocket_url=f"{websocket_url}/?results_id={results_id}",
            ), 202
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except Exception:
            LOG.exception("Could not queue code review")
            return jsonify(error="Could not queue code review; see server logs"), 500
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            if retained_workspace is not None and not queued:
                shutil.rmtree(retained_workspace, ignore_errors=True)
                if 'results_id' in locals():
                    jobs.delete(results_id)

    @app.get("/report")
    def report():
        results_id = (request.args.get("results_id") or "").strip()
        output_type = (request.args.get("type") or "html").strip().lower()
        if len(results_id) != 32 or any(c not in "0123456789abcdef" for c in results_id):
            return jsonify(error="Invalid results_id"), 400
        if output_type not in {"html", "plain", "markdown"}:
            return jsonify(error="type must be html, plain, or markdown"), 400
        html_path = report_dir / f"{results_id}.html"
        markdown_path = report_dir / f"{results_id}.md"
        job = jobs.get(results_id)
        if html_path.is_file():
            if output_type == "html":
                content = enhance_existing_report_html(
                    html_path.read_text(encoding="utf-8"),
                    results_id,
                    status=str(job.get("status", "completed")) if job else "completed",
                )
                return content, 200, {"Content-Type": "text/html; charset=utf-8"}
            if not markdown_path.is_file():
                return jsonify(error="Requested report format is unavailable"), 404
            content = markdown_path.read_text(encoding="utf-8")
            if output_type == "plain":
                return markdown_to_plain(content), 200, {"Content-Type": "text/plain; charset=utf-8"}
            return content, 200, {"Content-Type": "text/markdown; charset=utf-8"}
        if job is None:
            return jsonify(error="Report not found"), 404
        job = _sync_rq_status(job)
        if output_type in {"plain", "markdown"}:
            content = render_job_markdown(job)
            if output_type == "plain":
                content = markdown_to_plain(content)
            mimetype = "text/plain" if output_type == "plain" else "text/markdown"
            return content, 200, {
                "Content-Type": f"{mimetype}; charset=utf-8",
                "Cache-Control": "no-store",
            }
        return render_job_page(job), 200, {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
        }

    @app.get("/report/status")
    def report_status():
        results_id = (request.args.get("results_id") or "").strip()
        if len(results_id) != 32 or any(c not in "0123456789abcdef" for c in results_id):
            return jsonify(error="Invalid results_id"), 400
        job = jobs.get(results_id)
        if job is None:
            return jsonify(error="Report not found"), 404
        job = _sync_rq_status(job)
        return jsonify(report_status_payload(job)), 200, {"Cache-Control": "no-store"}

    @app.post("/report/stop")
    def stop_report():
        json_body = request.get_json(silent=True) or {} if request.is_json else {}
        results_id = str(
            request.form.get("results_id")
            or request.args.get("results_id")
            or json_body.get("results_id")
            or ""
        ).strip()
        if len(results_id) != 32 or any(c not in "0123456789abcdef" for c in results_id):
            return jsonify(error="Invalid results_id"), 400

        job = jobs.get(results_id)
        if job is None:
            return jsonify(error="Report not found"), 404
        job = _sync_rq_status(job)
        if job.get("status") not in {"queued", "running"}:
            return jsonify(error="Review is not queued or running", status=job.get("status")), 409

        try:
            rq_job = RQJob.fetch(results_id, connection=rq_redis)
            rq_status = rq_job.get_status(refresh=True)
            rq_status = getattr(rq_status, "value", str(rq_status))
            if rq_status == "started":
                send_stop_job_command(rq_redis, results_id)
                updated = jobs.update(
                    results_id,
                    progress="Stop requested; waiting for the worker to terminate the review.",
                )
                if not request.is_json:
                    return redirect(f"/report?results_id={results_id}", code=303)
                return jsonify(report_status_payload(updated)), 202
            if rq_status in {"queued", "deferred", "scheduled"}:
                rq_job.cancel()
                shutil.rmtree(workspace_dir / results_id, ignore_errors=True)
                updated = jobs.update(
                    results_id,
                    status="stopped",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    progress="Review stopped before it started.",
                    exception="",
                    workspace_available=False,
                )
                if not request.is_json:
                    return redirect(f"/report?results_id={results_id}", code=303)
                return jsonify(report_status_payload(updated)), 200
        except NoSuchJobError:
            job = _sync_rq_status(job)
            return jsonify(error="Background job was not found", status=job.get("status")), 409
        except InvalidJobOperation:
            job = _sync_rq_status(jobs.get(results_id) or job)
            return jsonify(error="Review could not be stopped in its current state", status=job.get("status")), 409

        job = _sync_rq_status(jobs.get(results_id) or job)
        return jsonify(error="Review could not be stopped in its current state", status=job.get("status")), 409

    @app.post("/report/reviewed")
    def mark_report_reviewed():
        json_body = request.get_json(silent=True) or {} if request.is_json else {}
        results_id = str(
            request.form.get("results_id")
            or request.args.get("results_id")
            or json_body.get("results_id")
            or ""
        ).strip()
        if len(results_id) != 32 or any(c not in "0123456789abcdef" for c in results_id):
            return jsonify(error="Invalid results_id"), 400
        job = jobs.get(results_id)
        if job is None:
            return jsonify(error="Report not found"), 404
        if job.get("status") != "completed":
            return jsonify(
                error="Only a completed report can be marked reviewed",
                status=job.get("status"),
            ), 409
        updated = jobs.update(results_id, status="reviewed")
        if request.is_json:
            return jsonify(report_status_payload(updated)), 200
        return redirect(request.form.get("next") or f"/report?results_id={results_id}", code=303)

    @app.route("/report/checklist", methods=["GET", "POST"])
    def report_checklist():
        json_body = request.get_json(silent=True) or {} if request.is_json else {}
        results_id = str(request.values.get("results_id") or json_body.get("results_id") or "").strip()
        if len(results_id) != 32 or any(c not in "0123456789abcdef" for c in results_id):
            return jsonify(error="Invalid results_id"), 400
        job = jobs.get(results_id)
        if job is None:
            return jsonify(error="Report not found"), 404
        if request.method == "GET":
            return jsonify(results_id=results_id, checklist=job.get("checklist", [])), 200, {
                "Cache-Control": "no-store"
            }
        checked = json_body.get("checked", [])
        if not isinstance(checked, list) or len(checked) > 10_000:
            return jsonify(error="checked must be a list"), 400
        checked = sorted({
            item for item in checked
            if isinstance(item, str) and re.fullmatch(r"item-\d+", item)
        })
        updated = jobs.update(results_id, checklist=checked)
        return jsonify(results_id=results_id, checklist=updated["checklist"], saved=True)

    @app.post("/report/delete")
    def delete_report():
        results_id = (request.form.get("results_id") or request.args.get("results_id") or "").strip()
        if len(results_id) != 32 or any(c not in "0123456789abcdef" for c in results_id):
            return jsonify(error="Invalid results_id"), 400
        job = jobs.get(results_id)
        if job is not None:
            job = _sync_rq_status(job)
        if job and job.get("status") in {"queued", "running"}:
            return jsonify(error="A queued or running review cannot be deleted"), 409
        try:
            RQJob.fetch(results_id, connection=rq_redis).delete()
        except NoSuchJobError:
            pass
        targets = [
            report_dir / f"{results_id}.html",
            report_dir / f"{results_id}.md",
            report_dir / f"{results_id}.json",
            job_dir / f"{results_id}.json",
            job_dir / f"{results_id}.json.migrated",
        ]
        deleted = jobs.delete(results_id)
        for target in targets:
            if target.is_file():
                target.unlink()
                deleted = True
        retained_workspace = workspace_dir / results_id
        if retained_workspace.is_dir():
            shutil.rmtree(retained_workspace)
            deleted = True
        if not deleted:
            return jsonify(error="Report not found"), 404
        return render_template("deleted.html"), 200

    return app
