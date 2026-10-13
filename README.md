# AI CICD Security Guardrail

A Flask API that accepts source files or archives, lets a LangGraph-orchestrated LangChain agent inspect them, and stores a sanitized HTML code-quality report.

## Setup

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
# Edit config.yaml and set model endpoints/keys.
# Starts the Flask development server and one RQ worker
./scripts/dev.sh
```

For a web-only development process, use `CODEQC_START_WORKER=0 ./scripts/dev.sh` and start one or more `./scripts/worker.sh` processes separately.

Redis defaults to `redis://localhost:6379/0`. For a simple local production-style launch, `./scripts/start-all.sh` starts Gunicorn and one RQ worker. Run `./scripts/start.sh` and one or more `./scripts/worker.sh` processes separately under a process supervisor for production. Environment options include `REDIS_URL`, `CODEQC_QUEUE`, `CODEQC_REDIS_PREFIX`, `CODEQC_JOB_TIMEOUT`, `CODEQC_CONFIG`, `CODEQC_BIND`, `CODEQC_WORKERS`, `CODEQC_REPORT_DIR`, and `CODEQC_JOB_DIR`. Protect this service with authentication and TLS before exposing it publicly.

`./scripts/start-all.sh` also starts the report-status WebSocket server on port `8081`. Connect with the `websocket_url` returned by `/api/codeqc`, or connect without a query string and send either the ID or `{"results_id":"..."}` as the first message. The server immediately sends the current `/report/status` JSON and sends a new snapshot after every report update (including progress/log trace and status changes). It closes normally (code `1000`) when the requested `status` query parameter (`completed` by default, or `reviewed`) is reached; `failed` and `stopped` close with code `1011`. Configure its listener with `CODEQC_WS_HOST` and `CODEQC_WS_PORT`. Set `CODEQC_WS_PUBLIC_URL` (for example, `wss://codeqc.example/ws`) when a reverse proxy exposes the WebSocket server at a different public URL.

## Set up GitLab CI

1. In the service's `config.yaml`, set `public_url.domain` to the hostname or IP
   reachable from the GitLab runner, `port` to the public HTTP port, `ws_port` to
   the public WebSocket port, and `docker_image` to your registry image reference.
   Set `scheme: https` if both endpoints use TLS. These settings configure the
   generated CI file, not the service listeners. Restart the web service after
   changing configuration. Older configurations without `public_url` default to
   localhost ports 8080/8081 and `codeqc:latest`; set deployment values before use.

2. Build and push the CI tools image from this project's root. Use the same image
   reference as `public_url.docker_image`:

   ```sh
   docker login registry.example.com
   ./docker/build.sh registry.example.com/your-group/codeqc:latest
   ```

   The script runs `docker build` using `docker/Dockerfile`, then `docker push`.
   This image contains the CI tools; run the API and workers separately using the
   setup steps above. The registry repository must exist or allow creation on
   push, and your account needs push access. Configure the GitLab runner with pull
   credentials for a private registry. Use a runner that supports job images and
   has the `code-qc` tag; it must reach the registry and both service ports.

3. Open `/generate-cicd` on the running service, select the pipeline rules and file
   patterns, and generate the file. Save the output as `.gitlab-ci.yml` in the root
   of the repository you want to review. `scripts/.gitlab-ci.yml` is a template;
   its placeholders are filled from configuration by the generator. If your
   repository already has a CI file, merge the generated stage, variables, hidden
   job and `code_qc` job into it.

4. Create an empty GitLab project and, from the source repository to review, run:

   ```sh
   git init                         # Skip if already a Git repository.
   git add .gitlab-ci.yml
   # Add the source files you want tracked too; keep credentials out of Git.
   git commit -m "Add code quality pipeline"
   git remote add origin git@gitlab.example.com:your-group/your-project.git
   git push -u origin HEAD
   ```

   Replace the remote URL with your project's clone URL. For an existing remote,
   keep it and use your normal push command. In GitLab, open the pipeline and
   manually start `code_qc`; generated jobs use `when: manual`. The web option
   also allows starting a pipeline from GitLab's **New pipeline** page.

## API

`GET /` redirects to the HTML report listing at `/reports`.

Upload one or multiple files (the field may be `files` or `file`). The API validates and stores the upload, queues an asynchronous LangGraph review, and responds immediately:

```sh
curl -X POST http://localhost:8080/api/codeqc \
  -F 'files=@src/app.py' \
  -F 'files=@tests/test_app.py' \
  -F 'model_name=local-coder' \
  -F 'type=all' \
  -F 'user_prompt=Pay special attention to input validation.' \
  -F 'prompt=@prompt.txt'
```

`model_name` and `review_type` may be supplied as multipart form fields or query-string parameters. The older `model` and `type` names remain supported. If `model_name` is omitted, the first model in `config.yaml` is used; the review type defaults to `all`.
The optional UTF-8 `prompt` file is appended to `user_prompt`; either field may be used independently. Their combined content may not exceed 10,000 characters.

When `scripts/qc.sh` runs in GitLab CI, set `PROMPT` to send inline additional instructions. If `PROMPT` is empty, `PROMPT_FILE` may name a prompt file beneath `CI_PROJECT_DIR`; `PROMPT` takes precedence when both are set. `CODEX_ACTION` defaults to `auto`; set it to `reviewed` to wait until the report is marked reviewed before continuing to Codex. Set `RESULTS_ID` to an existing 32-character result ID to skip file collection and AI CICD Security Guardrail submission, wait for that result, and then run the Codex action. Resume mode derives the WebSocket address using `CODEQC_WS_PORT` (default `8081`); set `CODEQC_WS_PUBLIC_URL` when the service uses a reverse-proxy WebSocket URL.

Archives supported: `.zip`, `.tar`, `.tar.gz`, and `.tgz`. A successful response is:

```json
{"results_id":"...", "status":"queued", "report_url":"/report?results_id=...", "websocket_url":"ws://localhost:8081/?results_id=..."}
```

Open `GET /report?results_id=...` immediately. While queued or running it displays progress and LangGraph trace activity. Once finished it displays the final HTML report.
Completed and failed result pages include a **Delete report** button. Active reviews cannot be deleted.
Deleting a report removes its HTML, Markdown, report metadata, Redis/RQ records, active workspace, and any legacy job metadata files under `data`.

The report endpoint supports three output formats through `type`:

```text
/report?results_id=...                 # HTML (default)
/report?results_id=...&type=html       # HTML
/report?results_id=...&type=plain      # text/plain
/report?results_id=...&type=markdown   # text/markdown
```

Open `GET /reports` for an HTML index of all existing jobs and reports. Results are sorted newest first and each result ID links to its report page.
The listing includes elapsed time and execution duration, and refreshes automatically every 10 seconds.

Report pages include the uploaded file list. Lists of ten files or fewer are expanded initially; larger lists are collapsed and can be expanded with the **Files** control. While a review runs, AJAX updates progress and tracing in place; the page reloads only when status changes. The file-list state is retained across those reloads. Failed reports also display their escaped exception traceback to aid troubleshooting.
Report details also show elapsed time, execution duration, and finished time, a collapsed LangGraph node/tool trace, and a link back to `/reports`. Uploaded workspaces are deleted when their RQ job completes, fails, stops, or is detected as abandoned. To run another review or use another model, upload the source again.

Review jobs are persisted in Redis and executed by RQ workers independently of Gunicorn. Queued jobs survive web-server restarts, and multiple Gunicorn processes share the same job state safely.

List the configured model names without exposing provider settings or API keys:

```sh
curl http://localhost:8080/models
```

```json
{"models":["local-coder","openai-example"]}
```

`GET /health` also returns service health and the configured model names.

## Configuration notes

- `model_name` must be unique and is also sent as the provider's model identifier.
- `base_url` should include `/v1` for OpenAI-compatible servers.
- `api_key` is optional; an internal placeholder is used when a server requires no authentication.
- Environment interpolation supports `${NAME}` and `${NAME:-default}`.
- Keep `verify_ssl: true` in production. Disable it only for a trusted self-signed development server.
- File inspection is provided by `@modelcontextprotocol/server-filesystem` over MCP stdio. Node.js and `npx` must be installed on every RQ worker host. The first review may download the npm package; pin the package version or preinstall it for reproducible/offline deployments.
- `filesystem_mcp.command` and `filesystem_mcp.args` configure how the server starts. AI CICD Security Guardrail always appends the active review workspace as its sole allowed directory.
- AI CICD Security Guardrail exposes only the MCP server's list, read, metadata, tree, and search tools to the model. Write, edit, move, and directory-creation tools are filtered out.
- The legacy `limits.max_file_read_kb` key is still accepted but no longer controls MCP tool responses.
- LangGraph explicitly manages the model → tools → model loop. `limits.recursion_limit` bounds graph execution and defaults to `60`.
- When the graph approaches `recursion_limit`, it disables further tool calls and asks the model to synthesize a final report from the evidence already collected. This prevents repetitive tool use from ending in a recursion-limit error. The older `max_agent_iterations` setting is still accepted when `recursion_limit` is absent.

Reports default to `data/reports`, Redis stores job metadata and queue state, and temporary workspaces use `data/jobs/workspaces` only while jobs are active. Active workspaces must be on storage shared by all web and RQ worker hosts. Report lifecycle cleanup and API authentication are deployment concerns and should be configured before public use.
