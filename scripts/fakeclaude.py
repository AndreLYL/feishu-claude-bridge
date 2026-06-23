"""离线测试桩：模拟 claude stream-json。每收到一条 user 消息，回放一遍
FAKE_FIXTURE 指向的 fixture（按行）。收到 control_response 则忽略继续。"""
import json, os, sys

def main():
    fixture = os.environ["FAKE_FIXTURE"]
    with open(fixture) as f:
        template_lines = [ln for ln in f.read().splitlines() if ln.strip()]
    for raw in iter(sys.stdin.readline, ""):
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if msg.get("type") == "control_response":
            continue  # 权限应答：继续等下一条
        for ln in template_lines:
            sys.stdout.write(ln + "\n")
            sys.stdout.flush()

if __name__ == "__main__":
    main()
