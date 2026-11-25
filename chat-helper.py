#!/usr/bin/env python3
"""xinwen-workbench 聊天 helper（服务器端，Rumi 轮询用）。

用法（都在服务器上跑，CHAT_DIR=/opt/xinwen-workbench/data/chat）：
  python3 chat-helper.py read <agent>              # 输出 {"ok":true,"messages":[...]}
  python3 chat-helper.py append <agent> <b64>     # b64 是 JSON {"text": "..."}，from=agent，输出 {"ok":true,"id":N}
  python3 chat-helper.py readmark <agent> <who> <up_to>
  python3 chat-helper.py typing <agent> <0|1>
  python3 chat-helper.py heartbeat <agent>         # 写 agent-bus/<agent>/status.json，last_seen=现在 UTC
文件格式与 app.py 完全一致：history 用 fcntl 锁 + 原子替换，read/typing/stream
用 chat/read|typing|stream/<agent>.json，心跳用 agent-bus/<agent>/status.json。
"""
import base64
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
# helper 放在 /opt/xinwen-workbench/ 时，数据在 ./data；放在 data/ 里时，数据就是 ./
CHAT_DIR = os.environ.get("WB_CHAT_DIR") or (
    os.path.join(BASE, "data", "chat") if os.path.isdir(os.path.join(BASE, "data")) else os.path.join(BASE, "chat")
)
# agent-bus 已与宿主机 /opt/shared/agent-bus 统一（容器内挂载到 /data/agent-bus）；优先用它，不存在时回落旧路径
BUS_DIR = os.environ.get("WB_BUS_DIR") or (
    "/opt/shared/agent-bus" if os.path.isdir("/opt/shared/agent-bus") else
    (os.path.join(BASE, "data", "agent-bus") if os.path.isdir(os.path.join(BASE, "data")) else os.path.join(BASE, "agent-bus"))
)
HISTORY_DIR = os.path.join(CHAT_DIR, "history")
LOCK_FILE = os.path.join(CHAT_DIR, "history.lock")


def _lock():
    os.makedirs(CHAT_DIR, exist_ok=True)
    os.makedirs(HISTORY_DIR, exist_ok=True)
    return open(LOCK_FILE, "w")


def _hist_path(agent):
    return os.path.join(HISTORY_DIR, agent + ".json")


def _load(agent):
    try:
        with open(_hist_path(agent), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _marker_path(kind, agent):
    return os.path.join(CHAT_DIR, kind, agent + ".json")


def _read_marker(kind, agent):
    try:
        with open(_marker_path(kind, agent), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def cmd_read(agent):
    print(json.dumps({"ok": True, "messages": _load(agent)}, ensure_ascii=False))
    return 0


def cmd_append(agent, b64):
    try:
        payload = json.loads(base64.b64decode(b64).decode("utf-8"))
        text = str(payload.get("text") or "")
    except Exception as e:
        print(json.dumps({"ok": False, "error": "bad payload: %s" % e}, ensure_ascii=False))
        return 1
    if not text:
        print(json.dumps({"ok": False, "error": "empty text"}, ensure_ascii=False))
        return 1
    lock = _lock()
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        msgs = _load(agent)
        nid = max((m.get("id", 0) for m in msgs), default=0) + 1
        msgs.append({"id": nid, "from": agent, "text": text, "ts": int(time.time())})
        _save_atomic(_hist_path(agent), msgs)
        print(json.dumps({"ok": True, "id": nid}, ensure_ascii=False))
        return 0
    finally:
        lock.close()


def cmd_readmark(agent, who, up_to):
    if who not in ("user", "agent"):
        print(json.dumps({"ok": False, "error": "bad who"}, ensure_ascii=False))
        return 1
    try:
        up_to = int(up_to)
    except Exception:
        print(json.dumps({"ok": False, "error": "bad up_to"}, ensure_ascii=False))
        return 1
    os.makedirs(os.path.join(CHAT_DIR, "read"), exist_ok=True)
    lock = _lock()
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        d = _read_marker("read", agent)
        if up_to > int(d.get(who) or 0):
            d[who] = up_to
            _save_atomic(_marker_path("read", agent), d)
        print(json.dumps({"ok": True, "read": d}, ensure_ascii=False))
        return 0
    finally:
        lock.close()


def cmd_typing(agent, val):
    os.makedirs(os.path.join(CHAT_DIR, "typing"), exist_ok=True)
    d = {"typing": bool(int(val)), "ts": int(time.time())}
    _save_atomic(_marker_path("typing", agent), d)
    print(json.dumps({"ok": True, "typing": d["typing"]}, ensure_ascii=False))
    return 0


def cmd_heartbeat(agent):
    d = os.path.join(BUS_DIR, agent)
    os.makedirs(d, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = {"agent": agent, "last_seen": now, "status": "poll"}
    _save_atomic(os.path.join(d, "status.json"), data)
    print(json.dumps({"ok": True, "last_seen": now}, ensure_ascii=False))
    return 0


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"ok": False, "error": "usage: chat-helper.py <read|append|readmark|typing|heartbeat> <agent> [args]"}, ensure_ascii=False))
        return 2
    cmd, agent = sys.argv[1], sys.argv[2]
    if cmd == "read":
        return cmd_read(agent)
    if cmd == "append":
        return cmd_append(agent, sys.argv[3] if len(sys.argv) > 3 else "")
    if cmd == "readmark":
        return cmd_readmark(agent, sys.argv[3], sys.argv[4])
    if cmd == "typing":
        return cmd_typing(agent, sys.argv[3])
    if cmd == "heartbeat":
        return cmd_heartbeat(agent)
    print(json.dumps({"ok": False, "error": "unknown cmd"}, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    sys.exit(main())
