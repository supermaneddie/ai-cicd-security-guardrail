from pathlib import Path
import unittest


class AgentGraphSourceTests(unittest.TestCase):
    def test_agent_uses_langgraph_state_graph(self):
        source = (Path(__file__).parents[1] / "codeqc" / "agent.py").read_text()
        self.assertIn("StateGraph(ReviewState)", source)
        self.assertIn("ToolNode(tools", source)
        self.assertIn("tools_condition", source)
        self.assertIn("async def run_review_async", source)
        self.assertIn("graph.astream(", source)
        self.assertIn("remaining_steps", source)
        self.assertIn("_invoke_logged", source)
        self.assertIn("final-synthesis", source)
        self.assertIn("final-retry", source)
        self.assertIn("config.limits.recursion_limit", source)
        self.assertIn("stdio_client(server_params)", source)
        self.assertIn("load_mcp_tools(session)", source)
        self.assertIn("READ_ONLY_FILESYSTEM_TOOLS", source)

    def test_models_endpoint_is_defined(self):
        source = (Path(__file__).parents[1] / "codeqc" / "app.py").read_text()
        self.assertIn('@app.get("/")', source)
        self.assertIn('redirect("/reports", code=302)', source)
        self.assertIn('@app.get("/models")', source)
        self.assertIn("jsonify(models=sorted(settings.models))", source)
        self.assertIn('@app.get("/reports")', source)
        self.assertIn('@app.route("/generate-cicd", methods=["GET", "POST"])', source)
        self.assertIn('"scripts" / ".gitlab-ci.yml"', source)

    def test_request_fallbacks_and_delete_endpoint_are_defined(self):
        source = (Path(__file__).parents[1] / "codeqc" / "app.py").read_text()
        self.assertIn("default_model = next(iter(settings.models))", source)
        self.assertIn('request.args.get("review_type")', source)
        self.assertIn('@app.post("/report/delete")', source)
        self.assertIn('@app.post("/report/stop")', source)
        self.assertIn("send_stop_job_command", source)
        self.assertIn('output_type not in {"html", "plain", "markdown"}', source)
        self.assertIn('render_template("deleted.html")', source)
        deleted_template = (Path(__file__).parents[1] / "codeqc" / "templates" / "deleted.html").read_text()
        self.assertIn('<a href="/reports">Back to report listing</a>', deleted_template)
        self.assertIn('@app.get("/report/status")', source)
        self.assertNotIn('@app.post("/report/rerun")', source)
        self.assertIn('job_dir / f"{results_id}.json.migrated"', source)

    def test_redis_rq_queue_is_configured(self):
        source = (Path(__file__).parents[1] / "codeqc" / "app.py").read_text()
        task_source = (Path(__file__).parents[1] / "codeqc" / "tasks.py").read_text()
        self.assertIn("RedisJobStore", source)
        self.assertIn("Queue(queue_name", source)
        self.assertIn("review_queue.enqueue(", source)
        self.assertIn("def execute_review_task(", task_source)
        dev_script = (Path(__file__).parents[1] / "scripts" / "dev.sh").read_text()
        self.assertIn("CODEQC_START_WORKER", dev_script)
        self.assertIn("./scripts/worker.sh", dev_script)


if __name__ == "__main__":
    unittest.main()
