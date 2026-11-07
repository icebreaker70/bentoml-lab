"""KServe 추론 로그 수신기.

KServe predictor 의 logger(agent)가 보내는 CloudEvent(요청/응답)를 받아
일정 개수 또는 일정 시간마다 JSONL 파일로 묶어 SeaweedFS(S3)에 저장한다.

  POST /                 KServe logger 이벤트 수신 (binary-mode CloudEvent)
  GET  /healthz          헬스 체크
  GET  /reports          드리프트 리포트 목록 (CronJob 이 S3 에 올린 HTML)
  GET  /reports/<name>   리포트 HTML 보기 (latest.html 은 가장 최근 리포트)
"""

import json
import os
import signal
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3

BUCKET = os.environ.get("S3_BUCKET", "mlpipeline")
PREFIX = os.environ.get("S3_PREFIX", "private-artifacts/kubeflow-user-example-com/drift-lab").rstrip("/")
FLUSH_EVENTS = int(os.environ.get("FLUSH_EVENTS", "50"))
FLUSH_SECONDS = float(os.environ.get("FLUSH_SECONDS", "30"))

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ.get("S3_ENDPOINT", "http://minio-service.kubeflow:9000"),
    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    region_name="us-east-1",
)

lock = threading.Lock()
buffer: list[dict] = []
last_flush = time.time()


def flush() -> None:
    """버퍼를 inference-logs/<isvc>/dt=YYYY-MM-DD/<시각>-<uuid>.jsonl 로 업로드한다."""
    global buffer, last_flush
    with lock:
        events, buffer, last_flush = buffer, [], time.time()
    if not events:
        return
    now = datetime.now(timezone.utc)
    by_isvc: dict[str, list[dict]] = {}
    for e in events:
        by_isvc.setdefault(e["isvc"] or "unknown", []).append(e)
    for isvc, rows in by_isvc.items():
        key = f"{PREFIX}/inference-logs/{isvc}/dt={now:%Y-%m-%d}/{now:%H%M%S}-{uuid.uuid4().hex[:8]}.jsonl"
        body = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
        s3.put_object(Bucket=BUCKET, Key=key, Body=body.encode())
        print(f"flushed {len(rows)} events -> s3://{BUCKET}/{key}", flush=True)


def flusher() -> None:
    while True:
        time.sleep(1)
        if buffer and time.time() - last_flush >= FLUSH_SECONDS:
            try:
                flush()
            except Exception as e:  # 업로드 실패가 수신을 막지 않게 한다
                print("flush error:", e, flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 요청마다 찍히는 기본 접근 로그는 끈다
        pass

    def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        h = self.headers
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw.decode(errors="replace")
        event = {
            "id": h.get("Ce-Id"),                              # 요청과 응답이 같은 id 를 공유한다
            "type": h.get("Ce-Type"),                          # org.kubeflow.serving.inference.request|response
            "time": h.get("Ce-Time") or datetime.now(timezone.utc).isoformat(),
            "isvc": h.get("Ce-Inferenceservicename"),
            "component": h.get("Ce-Component"),
            "endpoint": h.get("Ce-Endpoint"),
            "payload": payload,
        }
        with lock:
            buffer.append(event)
            n = len(buffer)
        if n >= FLUSH_EVENTS:
            flush()
        self._send(202, b"accepted")

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, b"ok")
        if self.path in ("/reports", "/reports/"):
            res = s3.list_objects_v2(Bucket=BUCKET, Prefix=f"{PREFIX}/reports/")
            names = sorted((o["Key"].rsplit("/", 1)[-1] for o in res.get("Contents", [])
                            if o["Key"].endswith(".html")), reverse=True)
            items = "".join(f'<li><a href="/reports/{n}">{n}</a></li>' for n in names)
            return self._send(200, f"<h1>Drift reports</h1><ul>{items}</ul>".encode(), "text/html; charset=utf-8")
        if self.path.startswith("/reports/"):
            name = self.path.split("/reports/", 1)[1]
            if "/" in name or not name.endswith(".html"):
                return self._send(400, b"bad name")
            try:
                obj = s3.get_object(Bucket=BUCKET, Key=f"{PREFIX}/reports/{name}")
            except s3.exceptions.NoSuchKey:
                return self._send(404, b"not found")
            return self._send(200, obj["Body"].read(), "text/html; charset=utf-8")
        self._send(404, b"not found")


def on_sigterm(*_):
    flush()  # 파드 종료 시 버퍼에 남은 이벤트를 잃지 않게 한다
    raise SystemExit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, on_sigterm)
    threading.Thread(target=flusher, daemon=True).start()
    print(f"listening :8080, s3://{BUCKET}/{PREFIX}/", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
