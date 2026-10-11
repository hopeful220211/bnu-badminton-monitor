#!/usr/bin/env python3
"""本地模拟的场馆预约接口，用来在拿到真实接口之前把监控流程跑通。

    python demo/demo_server.py          # 监听 127.0.0.1:8765

POST /api/venue/slots  body: {"date": "YYYY-MM-DD"}
需要 Cookie: SESSION=demo，否则返回 {"code": 401, "msg": "未登录"}。
每次请求有小概率随机「退订」或「被订走」一个场地，模拟全天有人退场。
"""

import json
import random
from http.server import BaseHTTPRequestHandler, HTTPServer

COURTS = [f"羽毛球{i}号场" for i in range(1, 7)]
TIMES = [f"{h:02d}:00-{h + 1:02d}:00" for h in range(8, 22)]
BOARD = {}  # date -> {(court, time): status}；0 空闲，1 已预约


def board(date):
    if date not in BOARD:
        rnd = random.Random(date)
        BOARD[date] = {(c, t): 0 if rnd.random() < 0.08 else 1 for c in COURTS for t in TIMES}
    b = BOARD[date]
    if random.random() < 0.3:  # 随机有人退订 / 抢走
        k = random.choice(list(b))
        b[k] = 1 - b[k]
    return b


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if "SESSION=demo" not in (self.headers.get("Cookie") or ""):
            payload = {"code": 401, "msg": "未登录或登录已过期", "data": None}
        else:
            b = board(body.get("date", ""))
            payload = {"code": 200, "msg": "success", "data": {"date": body.get("date"), "courts": [
                {"id": i + 1, "name": c, "slots": [{"time": t, "status": b[(c, t)]} for t in TIMES]}
                for i, c in enumerate(COURTS)]}}
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print("模拟接口已启动：http://127.0.0.1:8765/api/venue/slots")
    HTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
