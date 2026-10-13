from pathlib import Path
import asyncio
from io import BytesIO
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

from werkzeug.datastructures import FileStorage

from codeqc.app import create_app, _combined_user_prompt, _default_listening_files, _generate_gitlab_ci
from codeqc.config import load_config
from codeqc.agent import READ_ONLY_FILESYSTEM_TOOLS, _describe_tool_call
from codeqc.report import checklist_script, enhance_existing_report_html, format_elapsed, job_polling_script, markdown_to_plain, render_completed_markdown, render_file_list, render_job_markdown, render_job_page, render_report_actions, render_reports_page, render_status, render_trace, write_report
from codeqc.workspace import extract_archive
from codeqc.status import TERMINAL_STATUSES, report_status_payload
from codeqc.websocket import _close_for_terminal_status, _valid_results_id


class CodeQCTests(unittest.TestCase):
    def test_uploaded_prompt_is_appended_to_form_prompt(self):
        prompt_file = FileStorage(
            stream=BytesIO(b"Focus on authorization."),
            filename="prompt.txt",
        )

        combined = _combined_user_prompt("Check input validation.", [prompt_file])

        self.assertEqual(
            combined,
            "Check input validation.\n\nFocus on authorization.",
        )

    def test_uploaded_prompt_must_be_utf8(self):
        prompt_file = FileStorage(
            stream=BytesIO(b"\xff"),
            filename="prompt.txt",
        )

        with self.assertRaisesRegex(ValueError, "UTF-8"):
            _combined_user_prompt("", [prompt_file])

    def test_websocket_status_contract(self):
        results_id = "a" * 32
        self.assertTrue(_valid_results_id(results_id))
        self.assertFalse(_valid_results_id("not-an-id"))
        payload = report_status_payload({
            "results_id": results_id,
            "status": "running",
            "progress": "Reading files",
            "trace": [{"message": "updated log"}],
        })
        self.assertEqual(payload["results_id"], results_id)
        self.assertEqual(payload["trace"][0]["message"], "updated log")
        self.assertEqual(TERMINAL_STATUSES, {"completed", "reviewed", "failed", "stopped"})

    def test_websocket_only_closes_normally_for_completed_report(self):
        websocket = AsyncMock()

        asyncio.run(_close_for_terminal_status(websocket, "completed"))
        websocket.close.assert_awaited_once_with(code=1000, reason="Report completed")

        websocket.reset_mock()
        asyncio.run(_close_for_terminal_status(websocket, "reviewed"))
        websocket.close.assert_not_awaited()

        asyncio.run(_close_for_terminal_status(websocket, "reviewed", "reviewed"))
        websocket.close.assert_awaited_once_with(code=1000, reason="Report reviewed")

        websocket.reset_mock()
        asyncio.run(_close_for_terminal_status(websocket, "completed", "reviewed"))
        websocket.close.assert_not_awaited()

        websocket.reset_mock()
        asyncio.run(_close_for_terminal_status(websocket, "failed"))
        websocket.close.assert_awaited_once_with(code=1011, reason="Report failed")

    def test_filesystem_mcp_tools_are_read_only(self):
        self.assertIn("read_text_file", READ_ONLY_FILESYSTEM_TOOLS)
        self.assertIn("search_files", READ_ONLY_FILESYSTEM_TOOLS)
        self.assertNotIn("write_file", READ_ONLY_FILESYSTEM_TOOLS)
        self.assertNotIn("edit_file", READ_ONLY_FILESYSTEM_TOOLS)

    def test_duplicate_models_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text("default_system_prompt: review\nmodels:\n  - {model_name: x, base_url: http://a/v1}\n  - {model_name: x, base_url: http://b/v1}\n")
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                load_config(config)

    def test_recursion_limit_defaults_to_sixty(self):
        config = load_config(Path(__file__).parents[1] / "config.example.yaml")
        self.assertEqual(config.limits.recursion_limit, 60)
        model = next(iter(config.models.values()))
        self.assertEqual(model.call_timeout_seconds, 300)
        self.assertEqual(model.final_extra_body, {})
        self.assertEqual(config.filesystem_mcp.command, "npx")
        self.assertIn("@modelcontextprotocol/server-filesystem", config.filesystem_mcp.args)

    def test_legacy_agent_iterations_are_converted(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                "default_system_prompt: review\n"
                "models:\n  - {model_name: x, base_url: http://a/v1}\n"
                "limits:\n  max_agent_iterations: 10\n"
            )
            config = load_config(config_path)
            self.assertEqual(config.limits.recursion_limit, 21)

    def test_zip_traversal_rejected(self):
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../escape.py", "bad")
            config = load_config(Path(__file__).parents[1] / "config.example.yaml")
            with self.assertRaisesRegex(ValueError, "Unsafe"):
                extract_archive(archive, root / "out", config.limits)

    def test_plain_and_markdown_job_rendering(self):
        job = {
            "results_id": "b" * 32,
            "status": "running",
            "progress": "Reading files",
            "trace": [],
        }
        rendered = render_job_markdown(job)
        self.assertIn("# AI CICD Security Guardrail report", rendered)
        plain = markdown_to_plain(rendered)
        self.assertIn("AI CICD Security Guardrail report", plain)
        self.assertNotIn("**", plain)

    def test_user_prompt_is_shown_in_report_details_when_present(self):
        job = {
            "results_id": "b" * 32,
            "status": "running",
            "progress": "Reading files",
            "trace": [],
            "user_prompt": "Focus on <authorization> checks.",
        }
        page = render_job_page(job)
        self.assertIn("<h2>User prompt</h2>", page)
        self.assertIn("Focus on &lt;authorization&gt; checks.", page)
        self.assertIn("## User prompt\n\nFocus on <authorization> checks.", render_job_markdown(job))

        page_without_prompt = render_job_page({**job, "user_prompt": ""})
        self.assertNotIn("<h2>User prompt</h2>", page_without_prompt)

    def test_empty_completed_review_reports_no_response(self):
        metadata = {
            "model_name": "test-model",
            "type": "all",
            "files": ["main.py"],
            "user_prompt": "Check boundary cases.",
        }
        markdown_report = render_completed_markdown("  \n", "a" * 32, metadata)
        self.assertIn("## Review\n\nNo response", markdown_report)

        with tempfile.TemporaryDirectory() as directory:
            report_dir = Path(directory)
            write_report(report_dir, "a" * 32, "  \n", metadata)
            html_report = (report_dir / f"{'a' * 32}.html").read_text()
            self.assertIn("<p>No response</p>", html_report)
            self.assertIn("<h2>User prompt</h2>", html_report)
            self.assertIn("Check boundary cases.", html_report)
            saved_markdown = (report_dir / f"{'a' * 32}.md").read_text()
            self.assertIn("## User prompt\n\nCheck boundary cases.", saved_markdown)
            self.assertIn("## Review\n\nNo response", saved_markdown)

    def test_report_listing_escapes_values_and_links_results(self):
        page = render_reports_page([{
            "results_id": "c" * 32,
            "status": "completed",
            "model_name": "<unsafe>",
            "type": "all",
            "created_at": "2026-08-17T00:00:00+00:00",
        }])
        self.assertIn(f'/report?results_id={"c" * 32}', page)
        self.assertIn("&lt;unsafe&gt;", page)
        self.assertNotIn("<unsafe>", page)
        self.assertIn("Elapsed Time", page)
        self.assertIn('http-equiv="refresh" content="10"', page)
        self.assertIn('<a href="/generate-cicd">Generate CICD File</a>', page)

    def test_gitlab_ci_generator_customizes_manual_rules(self):
        template = (Path(__file__).parents[1] / "scripts" / ".gitlab-ci.yml").read_text()
        defaults = _default_listening_files(template)
        self.assertIn("**/*.py", defaults)

        generated = _generate_gitlab_ci(template, True, ["src/**/*.py", "docs/*.md"], True)
        self.assertIn('if: $CI_PIPELINE_SOURCE != "merge_request_event"', generated)
        self.assertIn("changes: &MONITOR_FILES", generated)
        self.assertIn("changes: *MONITOR_FILES", generated)
        self.assertIn("        MONITOR_FILES: |", generated)
        self.assertIn("          src/**/*.py", generated)
        self.assertIn("          docs/*.md", generated)
        self.assertIn('- "src/**/*.py"', generated)
        self.assertIn('- "docs/*.md"', generated)
        self.assertIn('if: $CI_PIPELINE_SOURCE == "web"', generated)
        self.assertEqual(generated.count("when: manual"), 2)
        self.assertNotIn('        - "**/*.py"', generated)

        web_only = _generate_gitlab_ci(template, False, [], True)
        self.assertNotIn("changes:", web_only)
        self.assertNotIn("MONITOR_FILES", web_only)
        self.assertEqual(web_only.count("when: manual"), 1)

        with self.assertRaisesRegex(ValueError, "at least one pipeline rule"):
            _generate_gitlab_ci(template, False, [], False)

    def test_generate_cicd_uses_configured_public_endpoints_and_image(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                "default_system_prompt: review\n"
                "models: [{model_name: test, base_url: http://localhost/v1}]\n"
                "public_url:\n"
                "  domain: qc.example.test\n"
                "  scheme: https\n"
                "  port: ${TEST_QC_PORT:-8443}\n"
                "  ws_port: 9443\n"
                "  docker_image: registry.example.test/team/qc:v2\n"
            )
            store = MagicMock()
            store.list_all.return_value = []
            with patch.dict("os.environ", {
                "CODEQC_CONFIG": str(config_path),
                "CODEQC_JOB_DIR": directory,
                "TEST_QC_PORT": "8443",
            }), patch("codeqc.app.Redis"), patch("codeqc.app.RedisJobStore", return_value=store), patch("codeqc.app.Queue"):
                client = create_app().test_client()
                self.assertEqual(client.get("/generate-cicd").status_code, 200)
                response = client.post("/generate-cicd", data={"web_pipeline": "on"})
            self.assertEqual(response.status_code, 200)
            generated = yaml.safe_load(response.text)
            self.assertEqual(generated["variables"]["CODE_QC_URL"], "https://qc.example.test:8443")
            self.assertEqual(generated["variables"]["CODE_QC_WS_URL"], "wss://qc.example.test:9443")
            self.assertEqual(generated["code_qc"]["image"], "registry.example.test/team/qc:v2")
            self.assertNotIn("__CODE_QC", response.text)

    def test_file_list_collapses_only_above_ten(self):
        short = render_file_list([f"file-{number}.py" for number in range(10)])
        long = render_file_list([f"file-{number}.py" for number in range(11)])
        self.assertIn('<details class="file-list" open>', short)
        self.assertIn('<details class="file-list">', long)
        self.assertNotIn('<details class="file-list" open>', long)
        self.assertIn('class="file-list"', long)

    def test_job_polling_uses_ajax_and_preserves_file_state(self):
        script = job_polling_script("a" * 32, "running")
        self.assertIn("fetch('/report/status?results_id='", script)
        self.assertIn("job.status !== initialStatus", script)
        self.assertIn("sessionStorage", script)
        self.assertNotIn("http-equiv", script)

    def test_failed_job_report_shows_exception(self):
        rendered = render_job_markdown({
            "results_id": "f" * 32,
            "status": "failed",
            "progress": "Review failed",
            "files": ["main.py"],
            "exception": "Traceback: example failure",
        })
        self.assertIn("## Files (1)", rendered)
        self.assertIn("## Exception", rendered)
        self.assertIn("example failure", rendered)

    def test_elapsed_trace_and_report_controls(self):
        elapsed = format_elapsed({
            "started_at": "2026-08-17T00:00:00+00:00",
            "finished_at": "2026-08-17T01:02:03+00:00",
        })
        self.assertEqual(elapsed, "01:02:03")
        trace = render_trace([{"time": "now", "message": "tools: completed read_file"}])
        self.assertIn("LangGraph trace (1)", trace)
        self.assertNotIn(" open", trace)
        actions = render_report_actions("a" * 32, False)
        self.assertLess(actions.index("Back to reports"), actions.index("Delete report"))
        self.assertIn("Save checklist", actions)
        self.assertNotIn("Rerun", actions)
        self.assertNotIn("Reviewed", actions)
        completed_actions = render_report_actions("a" * 32, False, status="completed")
        self.assertIn('action="/report/reviewed"', completed_actions)
        self.assertIn("Reviewed", completed_actions)
        reviewed_actions = render_report_actions("a" * 32, False, status="reviewed")
        self.assertNotIn("Reviewed", reviewed_actions)
        active_actions = render_report_actions("a" * 32, True)
        self.assertIn('action="/report/stop"', active_actions)
        self.assertIn("Stop review", active_actions)

    def test_report_checklist_enhances_review_items_and_saves_to_server(self):
        script = checklist_script("a" * 32)
        self.assertIn("document.querySelectorAll('main h3')", script)
        self.assertIn("closest('.review-check-item')", script)
        self.assertNotIn("document.querySelectorAll('main li')", script)
        self.assertIn("/report/checklist", script)
        self.assertIn("data-checklist-id", script)
        self.assertIn("checklistIsDirty", script)
        self.assertIn("Save checklist changes before marking this report reviewed", script)
        self.assertIn("reviewedForms.forEach", script)
        old_report = "<html><head></head><body><nav class=\"report-actions\"></nav><main><h3>1.1 Issue</h3><ul><li>Severity: Critical</li></ul></main></body></html>"
        upgraded = enhance_existing_report_html(old_report, "a" * 32)
        self.assertIn("Save checklist", upgraded)
        self.assertIn('data-codeqc-checklist="2"', upgraded)
        legacy = old_report.replace(
            "</body>",
            "<script data-codeqc-checklist>const selector = 'main li';</script></body>",
        )
        upgraded_legacy = enhance_existing_report_html(legacy, "a" * 32)
        self.assertIn('data-codeqc-checklist="2"', upgraded_legacy)
        self.assertIn("document.querySelectorAll('main h3')", upgraded_legacy)
        self.assertNotIn("const selector = 'main li'", upgraded_legacy)
        duplicated = old_report.replace(
            "</body>", '<nav class="report-actions">Footer</nav></body>'
        )
        upgraded_layout = enhance_existing_report_html(duplicated, "a" * 32)
        self.assertEqual(upgraded_layout.count('class="report-actions"'), 1)
        self.assertIn("position: sticky", upgraded_layout)

    def test_running_status_has_spinner(self):
        self.assertIn('class="spinner"', render_status("running"))
        self.assertNotIn('class="spinner"', render_status("completed"))

    def test_tool_trace_descriptions_include_safe_arguments(self):
        self.assertEqual(
            _describe_tool_call({
                "name": "read_file",
                "args": {"path": "src/app.py", "start_line": 20, "end_line": 80},
            }),
            "read_file(path=src/app.py, lines=20-80)",
        )
        self.assertEqual(
            _describe_tool_call({"name": "read_file", "args": {"path": "src/app.py"}}),
            "read_file(path=src/app.py, lines=1-EOF)",
        )
        self.assertEqual(
            _describe_tool_call({"name": "list_workspace_files", "args": {}}),
            "list_workspace_files(path=.)",
        )
        self.assertNotIn("\n", _describe_tool_call({
            "name": "search_code", "args": {"query": "first\nsecond"}
        }))

if __name__ == "__main__":
    unittest.main()
