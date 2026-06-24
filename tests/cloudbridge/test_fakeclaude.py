import json, os, subprocess, sys
from pathlib import Path

def test_fakeclaude_replays_fixture_per_turn(tmp_path):
    fixture = tmp_path / "fix.jsonl"
    fixture.write_text(
        json.dumps({"type": "system", "subtype": "init", "session_id": "S"}) + "\n" +
        json.dumps({"type": "result", "subtype": "success", "result": "hi",
                    "session_id": "S", "usage": {}, "cost_usd": 0.0}) + "\n"
    )
    env = {**os.environ, "FAKE_FIXTURE": str(fixture)}
    proc = subprocess.Popen(
        [sys.executable, "scripts/fakeclaude.py"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1, env=env)
    proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": "go"}}) + "\n")
    proc.stdin.flush()
    lines = [proc.stdout.readline() for _ in range(2)]
    proc.stdin.close(); proc.wait(timeout=5)
    assert json.loads(lines[0])["type"] == "system"
    assert json.loads(lines[1])["subtype"] == "success"
