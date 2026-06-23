"""
手动 spike：实测 claude stream-json 行为，录制 fixture。非 CI。
用法: python scripts/spike_stream_json.py <out_dir>

本脚本逐一运行多个探针，每个探针对应一个检查项。
所有子进程都用 timeout 限制、短 prompt 节省费用。
"""
import json
import os
import subprocess
import sys
import threading
import time
import uuid


FIXTURE_DIR = sys.argv[1] if len(sys.argv) > 1 else "tests/cloudbridge/fixtures"
SPIKE_CWD = "/tmp/spike-cwd"
os.makedirs(FIXTURE_DIR, exist_ok=True)
os.makedirs(SPIKE_CWD, exist_ok=True)

RESULTS = {}


def pump_stdout(stream, lines_list, label="OUT"):
    """Read lines from stream, store and print them."""
    for raw in iter(stream.readline, ""):
        lines_list.append(raw.rstrip())
        print(f"[{label}] {raw.rstrip()}", flush=True)


def pump_stderr(stream, label="ERR"):
    """Read stderr and print it."""
    for raw in iter(stream.readline, ""):
        print(f"[{label}] {raw.rstrip()}", flush=True)


def send_msg(proc, text):
    msg = {"type": "user", "message": {"role": "user", "content": text}}
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def write_fixture(name, lines):
    path = os.path.join(FIXTURE_DIR, name)
    with open(path, "w") as f:
        for line in lines:
            line = line.strip()
            if line:
                f.write(line + "\n")
    print(f"[SPIKE] Wrote fixture: {path} ({len(lines)} lines)")
    return path


def parse_events(lines):
    events = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events


# ──────────────────────────────────────────────────────────────────────────────
# PROBE 1: Basic text turn (no --include-partial-messages)
# Captures: system/init shape, assistant event shape, result shape, session_id
# Also checks M2 (MCP autodiscovery, --bare not set)
# Also checks R3 (session_id matches --session-id)
# Also checks M1 (--replay-user-messages echo shape)
# ──────────────────────────────────────────────────────────────────────────────
def probe_text_turn_no_partial():
    print("\n" + "="*70)
    print("PROBE 1: Text turn WITHOUT --include-partial-messages")
    print("="*70)
    sid = str(uuid.uuid4())
    argv = [
        "timeout", "60",
        "claude", "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--replay-user-messages",
        "--session-id", sid,
    ]
    print("CMD:", " ".join(argv))
    lines = []
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, cwd=SPIKE_CWD,
    )
    t_out = threading.Thread(target=pump_stdout, args=(proc.stdout, lines, "P1-OUT"), daemon=True)
    t_err = threading.Thread(target=pump_stderr, args=(proc.stderr, "P1-ERR"), daemon=True)
    t_out.start(); t_err.start()

    time.sleep(2)  # wait for init
    send_msg(proc, "Reply with exactly one word: Hello")
    time.sleep(20)
    proc.stdin.close()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

    write_fixture("turn_text_no_partial.jsonl", lines)

    events = parse_events(lines)
    RESULTS["P1_events"] = events
    RESULTS["P1_sid"] = sid

    # Analyze
    event_types = [e.get("type") for e in events]
    print(f"\n[P1 ANALYSIS] event types seen: {event_types}")

    sys_events = [e for e in events if e.get("type") == "system"]
    result_events = [e for e in events if e.get("type") == "result"]
    user_events = [e for e in events if e.get("type") == "user"]
    assistant_events = [e for e in events if e.get("type") == "assistant"]

    print(f"[P1] system events: {len(sys_events)}")
    print(f"[P1] result events: {len(result_events)}")
    print(f"[P1] user events (replay): {len(user_events)}")
    print(f"[P1] assistant events: {len(assistant_events)}")

    if sys_events:
        s = sys_events[0]
        print(f"[P1] system/init keys: {list(s.keys())}")
        print(f"[P1] system session_id: {s.get('session_id')}")
        print(f"[P1] system mcp_servers: {s.get('mcp_servers')}")
        print(f"[P1] system tools count: {len(s.get('tools', []))}")
        print(f"[P1] system model: {s.get('model')}")
        print(f"[P1] permissionMode: {s.get('permissionMode')}")
        RESULTS["M2_system_event"] = s
        RESULTS["R3_system_sid"] = s.get("session_id")
        RESULTS["R3_passed_sid"] = sid
        RESULTS["R3_match"] = (s.get("session_id") == sid)

    if result_events:
        r = result_events[-1]
        print(f"[P1] result keys: {list(r.keys())}")
        print(f"[P1] result subtype: {r.get('subtype')}")
        print(f"[P1] result total_cost_usd: {r.get('total_cost_usd')}")
        print(f"[P1] result cost_usd: {r.get('cost_usd')}")
        print(f"[P1] result duration_ms: {r.get('duration_ms')}")
        print(f"[P1] result session_id: {r.get('session_id')}")
        RESULTS["C3_result_subtype"] = r.get("subtype")
        RESULTS["R3_result_sid"] = r.get("session_id")

    if user_events:
        print(f"[P1] M1 user replay event shape: {json.dumps(user_events[0], ensure_ascii=False)[:200]}")
        RESULTS["M1_user_replay"] = user_events[0]

    if assistant_events:
        a = assistant_events[0]
        print(f"[P1] assistant event keys: {list(a.keys())}")
        print(f"[P1] assistant content type: {type(a.get('message', {}).get('content'))}")
        RESULTS["assistant_event"] = a


# ──────────────────────────────────────────────────────────────────────────────
# PROBE 2: Text turn WITH --include-partial-messages
# Captures: stream_event / content_block_delta shape
# ──────────────────────────────────────────────────────────────────────────────
def probe_text_turn_with_partial():
    print("\n" + "="*70)
    print("PROBE 2: Text turn WITH --include-partial-messages")
    print("="*70)
    sid = str(uuid.uuid4())
    argv = [
        "timeout", "60",
        "claude", "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--replay-user-messages",
        "--session-id", sid,
    ]
    print("CMD:", " ".join(argv))
    lines = []
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, cwd=SPIKE_CWD,
    )
    t_out = threading.Thread(target=pump_stdout, args=(proc.stdout, lines, "P2-OUT"), daemon=True)
    t_err = threading.Thread(target=pump_stderr, args=(proc.stderr, "P2-ERR"), daemon=True)
    t_out.start(); t_err.start()

    time.sleep(2)
    send_msg(proc, "Reply with exactly one word: Hello")
    time.sleep(20)
    proc.stdin.close()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

    write_fixture("turn_text_with_partial.jsonl", lines)

    events = parse_events(lines)
    RESULTS["P2_events"] = events

    event_types = [e.get("type") for e in events]
    print(f"\n[P2 ANALYSIS] event types seen: {event_types}")

    stream_events = [e for e in events if e.get("type") == "stream_event"]
    print(f"[P2] stream_event count: {len(stream_events)}")
    if stream_events:
        # Show first few
        for se in stream_events[:5]:
            print(f"[P2] stream_event: {json.dumps(se, ensure_ascii=False)[:200]}")
        RESULTS["stream_event_sample"] = stream_events[:3]

    # Check for text_delta events
    delta_events = [e for e in events if e.get("type") == "stream_event"
                    and e.get("event", {}).get("type") == "content_block_delta"]
    print(f"[P2] content_block_delta count: {len(delta_events)}")
    if delta_events:
        RESULTS["content_block_delta_sample"] = delta_events[0]


# ──────────────────────────────────────────────────────────────────────────────
# PROBE 3: Permission test with --permission-prompt-tool stdio
# C1: Does control_request appear on stdout?
# ──────────────────────────────────────────────────────────────────────────────
def probe_permission_stdio():
    print("\n" + "="*70)
    print("PROBE 3: Permission test with --permission-prompt-tool stdio")
    print("="*70)
    sid = str(uuid.uuid4())
    argv = [
        "timeout", "60",
        "claude", "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-prompt-tool", "stdio",
        "--session-id", sid,
    ]
    print("CMD:", " ".join(argv))
    lines = []
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, cwd=SPIKE_CWD,
    )
    t_out = threading.Thread(target=pump_stdout, args=(proc.stdout, lines, "P3-OUT"), daemon=True)
    t_err = threading.Thread(target=pump_stderr, args=(proc.stderr, "P3-ERR"), daemon=True)
    t_out.start(); t_err.start()

    time.sleep(2)
    # Ask for a file creation — this should require permission
    send_msg(proc, "Write the literal text hi to /tmp/spike_test.txt using the Write tool")
    time.sleep(25)
    proc.stdin.close()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

    write_fixture("permission.jsonl", lines)

    events = parse_events(lines)
    RESULTS["P3_events"] = events

    event_types = [e.get("type") for e in events]
    print(f"\n[P3 ANALYSIS] event types seen: {event_types}")

    control_events = [e for e in events if "control" in e.get("type", "").lower()]
    print(f"[P3] control events: {control_events}")

    # Check if file was silently created
    file_created = os.path.exists("/tmp/spike_test.txt")
    print(f"[P3] /tmp/spike_test.txt was created (silent exec): {file_created}")

    RESULTS["C1_control_events"] = control_events
    RESULTS["C1_file_created_silently"] = file_created
    RESULTS["C1_event_types"] = event_types

    if control_events:
        print(f"[P3] control_request shape: {json.dumps(control_events[0], ensure_ascii=False)[:400]}")
        RESULTS["C1_control_request_shape"] = control_events[0]
    else:
        print("[P3] NO control_request found — stdio may be silently executing (bug #34046)")


# ──────────────────────────────────────────────────────────────────────────────
# PROBE 4: C2 — mid-turn second message
# Start a slightly longer response then immediately send a second message
# ──────────────────────────────────────────────────────────────────────────────
def probe_mid_turn_write():
    print("\n" + "="*70)
    print("PROBE 4: C2 — mid-turn second message test")
    print("="*70)
    sid = str(uuid.uuid4())
    argv = [
        "timeout", "90",
        "claude", "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--session-id", sid,
    ]
    print("CMD:", " ".join(argv))
    lines = []
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, cwd=SPIKE_CWD,
    )
    t_out = threading.Thread(target=pump_stdout, args=(proc.stdout, lines, "P4-OUT"), daemon=True)
    t_err = threading.Thread(target=pump_stderr, args=(proc.stderr, "P4-ERR"), daemon=True)
    t_out.start(); t_err.start()

    time.sleep(2)
    # Send first message that produces multi-line output
    send_msg(proc, "Count from 1 to 15, one per line.")
    time.sleep(3)  # Give it just enough time to start but not finish
    # Immediately send a second message mid-turn
    send_msg(proc, "Second message sent mid-turn — just say OK")
    print("[P4] Sent second message mid-turn, waiting 40s...")
    time.sleep(50)
    proc.stdin.close()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        print("[P4] Process killed (hung)")
        RESULTS["C2_hung"] = True

    write_fixture("mid_turn_test.jsonl", lines)

    events = parse_events(lines)
    RESULTS["P4_events"] = events

    event_types = [e.get("type") for e in events]
    print(f"\n[P4 ANALYSIS] event types: {event_types}")

    result_events = [e for e in events if e.get("type") == "result"]
    print(f"[P4] result events count: {len(result_events)}")
    RESULTS["C2_result_count"] = len(result_events)

    if len(result_events) >= 2:
        print("[P4] Two result events seen — second message may have been processed separately")
        RESULTS["C2_two_results"] = True
    elif len(result_events) == 1:
        print("[P4] Only one result — second message may have been folded into first turn or ignored")
        RESULTS["C2_one_result"] = True

    # Look for the second message content in assistant output
    assistant_events = [e for e in events if e.get("type") == "assistant"]
    for ae in assistant_events:
        content = ae.get("message", {}).get("content", [])
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                print(f"[P4] assistant text: {block.get('text', '')[:200]}")


# ──────────────────────────────────────────────────────────────────────────────
# PROBE 5: Tool use turn — capture tool_use event shape
# ──────────────────────────────────────────────────────────────────────────────
def probe_tool_use():
    print("\n" + "="*70)
    print("PROBE 5: Tool use turn — capture tool_use shape")
    print("="*70)
    sid = str(uuid.uuid4())
    argv = [
        "timeout", "60",
        "claude", "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--session-id", sid,
    ]
    print("CMD:", " ".join(argv))
    lines = []
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, cwd=SPIKE_CWD,
    )
    t_out = threading.Thread(target=pump_stdout, args=(proc.stdout, lines, "P5-OUT"), daemon=True)
    t_err = threading.Thread(target=pump_stderr, args=(proc.stderr, "P5-ERR"), daemon=True)
    t_out.start(); t_err.start()

    time.sleep(2)
    # Ask something that uses a tool (read a file)
    send_msg(proc, "Read the file /etc/hostname and tell me the hostname.")
    time.sleep(30)
    proc.stdin.close()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

    write_fixture("turn_tool.jsonl", lines)

    events = parse_events(lines)
    RESULTS["P5_events"] = events

    event_types = [e.get("type") for e in events]
    print(f"\n[P5 ANALYSIS] event types: {event_types}")

    # Look for tool_use in stream_events or assistant events
    for e in events:
        etype = e.get("type")
        if etype == "stream_event":
            inner = e.get("event", {})
            if inner.get("type") in ("content_block_start", "content_block_delta"):
                block = inner.get("content_block") or inner.get("delta") or {}
                if block.get("type") == "tool_use":
                    print(f"[P5] tool_use block: {json.dumps(e, ensure_ascii=False)[:300]}")
                    RESULTS["tool_use_event"] = e
        elif etype == "assistant":
            content = e.get("message", {}).get("content", [])
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    print(f"[P5] tool_use in assistant: {json.dumps(block, ensure_ascii=False)[:300]}")
                    RESULTS["tool_use_block"] = block


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*70}")
    print(f"SPIKE: claude stream-json behavior probe")
    print(f"Fixture dir: {FIXTURE_DIR}")
    print(f"Spike CWD: {SPIKE_CWD}")
    print(f"{'='*70}\n")

    probe_text_turn_no_partial()
    probe_text_turn_with_partial()
    probe_permission_stdio()
    probe_mid_turn_write()
    probe_tool_use()

    # Write summary
    summary_path = os.path.join(FIXTURE_DIR, "spike_summary.json")
    with open(summary_path, "w") as f:
        # Only serialize serializable parts
        summary = {
            "R3_session_id_match": RESULTS.get("R3_match"),
            "R3_passed_sid": RESULTS.get("R3_passed_sid"),
            "R3_system_sid": RESULTS.get("R3_system_sid"),
            "R3_result_sid": RESULTS.get("R3_result_sid"),
            "C1_file_created_silently": RESULTS.get("C1_file_created_silently"),
            "C1_control_events_count": len(RESULTS.get("C1_control_events", [])),
            "C1_event_types": RESULTS.get("C1_event_types"),
            "C2_result_count": RESULTS.get("C2_result_count"),
            "C2_hung": RESULTS.get("C2_hung", False),
            "C3_result_subtype": RESULTS.get("C3_result_subtype"),
            "M1_user_replay": RESULTS.get("M1_user_replay"),
            "M2_model": RESULTS.get("M2_system_event", {}).get("model") if RESULTS.get("M2_system_event") else None,
            "M2_mcp_servers": RESULTS.get("M2_system_event", {}).get("mcp_servers") if RESULTS.get("M2_system_event") else None,
            "M2_tools_count": len(RESULTS.get("M2_system_event", {}).get("tools", [])) if RESULTS.get("M2_system_event") else 0,
            "M2_permissionMode": RESULTS.get("M2_system_event", {}).get("permissionMode") if RESULTS.get("M2_system_event") else None,
        }
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"\n[SPIKE] Summary written to {summary_path}")
    print("\n[SPIKE COMPLETE]")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
