"""Evidently 드리프트 리포트 작업 (CronJob 에서 실행).

  python drift_job.py reference   기준(reference) 데이터를 S3 에 올린다 (최초 1회)
  python drift_job.py report      최근 WINDOW_MINUTES 동안의 추론 로그로 드리프트 리포트를 만든다

S3 레이아웃 (s3://$S3_BUCKET/$S3_PREFIX/)
  reference/iris.csv                         기준 데이터 (특성 4개 + prediction)
  inference-logs/<isvc>/dt=YYYY-MM-DD/*.jsonl  inference-logger 가 저장한 요청/응답 이벤트
  reports/drift-<UTC시각>.html|.json          리포트와 요약
  reports/latest.html                        가장 최근 리포트
  reports/_state.json                        마지막 리포트 시각 (다음 회차는 그 이후 로그만 본다)
"""

import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import boto3
import pandas as pd

FEATURES = ["sepal_length", "sepal_width", "petal_length", "petal_width"]

BUCKET = os.environ.get("S3_BUCKET", "mlpipeline")
PREFIX = os.environ.get("S3_PREFIX", "private-artifacts/kubeflow-user-example-com/drift-lab").rstrip("/")
ISVC = os.environ.get("ISVC_NAME", "iris-drift")
WINDOW_MINUTES = int(os.environ.get("WINDOW_MINUTES", "60"))  # 최대 조회 범위 (직전 리포트 이후 ∩ 최근 N분)
MIN_ROWS = int(os.environ.get("MIN_ROWS", "30"))
DRIFT_SHARE = float(os.environ.get("DRIFT_SHARE", "0.5"))  # 드리프트 컬럼 비율이 이 이상이면 데이터셋 드리프트
PUSHGATEWAY = os.environ.get("PUSHGATEWAY_URL", "")        # 예: http://prometheus-prometheus-pushgateway.prometheus:9091
MLFLOW_URI = os.environ.get("MLFLOW_TRACKING_URI", "")     # 예: http://mlflow.mlflow.svc.cluster.local

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ.get("S3_ENDPOINT", "http://minio-service.kubeflow:9000"),
    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    region_name="us-east-1",
)


def make_reference() -> None:
    """Iris 학습 데이터를 기준 데이터로 저장한다.

    예측 분포의 기준으로는 레이블(target)을 쓴다. 학습 데이터에서 모델 정확도가 거의 100%라
    '정상일 때의 예측 분포'와 사실상 같다.
    """
    from sklearn.datasets import load_iris

    df = load_iris(as_frame=True).frame
    df.columns = FEATURES + ["prediction"]
    key = f"{PREFIX}/reference/iris.csv"
    s3.put_object(Bucket=BUCKET, Key=key, Body=df.to_csv(index=False).encode())
    print(f"reference: {len(df)} rows -> s3://{BUCKET}/{key}")


def load_current(since: datetime) -> pd.DataFrame:
    """since 이후에 저장된 로그 파일을 읽어 요청(instances)과 응답(predictions)을 id 로 짝짓는다."""
    days = {(since + timedelta(days=i)).date() for i in range((datetime.now(timezone.utc) - since).days + 1)}
    days.add(datetime.now(timezone.utc).date())
    requests, responses = {}, {}
    files = 0
    for day in sorted(days):
        prefix = f"{PREFIX}/inference-logs/{ISVC}/dt={day:%Y-%m-%d}/"
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                if obj["LastModified"] < since:
                    continue
                files += 1
                body = s3.get_object(Bucket=BUCKET, Key=obj["Key"])["Body"].read().decode()
                for line in body.splitlines():
                    e = json.loads(line)
                    if not isinstance(e.get("payload"), dict):
                        continue
                    if e["type"].endswith(".request"):
                        requests[e["id"]] = e["payload"].get("instances", [])
                    elif e["type"].endswith(".response"):
                        responses[e["id"]] = e["payload"].get("predictions", [])

    rows = []
    for rid, instances in requests.items():
        preds = responses.get(rid)
        if preds is None or len(preds) != len(instances):
            continue  # 응답이 없거나(오류 등) 개수가 다르면 버린다
        rows += [list(x) + [p] for x, p in zip(instances, preds)]
    print(f"logs: files={files} requests={len(requests)} responses={len(responses)} rows={len(rows)}")
    return pd.DataFrame(rows, columns=FEATURES + ["prediction"])


def summarize(snapshot) -> dict:
    """Evidently 결과(dict)에서 알림/대시보드에 쓸 값만 뽑는다."""
    out = {"columns": {}, "tests": {}}
    for m in snapshot.dict()["metrics"]:
        cfg = m["config"]
        if cfg["type"].endswith("DriftedColumnsCount"):
            out["drifted_columns"] = int(m["value"]["count"])
            out["drift_share"] = float(m["value"]["share"])
        elif cfg["type"].endswith("ValueDrift"):
            out["columns"][cfg["column"]] = {"method": cfg["method"], "threshold": cfg["threshold"],
                                             "score": float(m["value"])}
    for t in snapshot.dict().get("tests", []):
        # DataSummaryPreset 의 자동 테스트(행 수 = 기준 ±10% 등)는 운영 구간 비교에 의미가 없어 뺀다.
        # (0.7.23 에서 DataSummaryPreset(include_tests=False)는 렌더링 중 KeyError 가 난다)
        if "Drift" in t["name"]:
            out["tests"][t["name"]] = t["status"]
    # p-value 방식은 score < threshold, 거리 방식은 score >= threshold 가 드리프트다
    for c in out["columns"].values():
        c["drift"] = c["score"] < c["threshold"] if "p_value" in c["method"] else c["score"] >= c["threshold"]
    out["dataset_drift"] = out["drift_share"] >= DRIFT_SHARE
    return out


def push_metrics(summary: dict, rows: int) -> None:
    import requests

    lines = [
        f"evidently_current_rows {rows}",
        f"evidently_drifted_columns {summary['drifted_columns']}",
        f"evidently_drift_share {summary['drift_share']}",
        f"evidently_dataset_drift {int(summary['dataset_drift'])}",
        f"evidently_last_run_timestamp_seconds {int(datetime.now(timezone.utc).timestamp())}",
    ]
    for col, c in summary["columns"].items():
        lines.append(f'evidently_column_drift_score{{column="{col}",method="{c["method"]}"}} {c["score"]}')
        lines.append(f'evidently_column_drift{{column="{col}"}} {int(c["drift"])}')
    url = f"{PUSHGATEWAY.rstrip('/')}/metrics/job/evidently_drift/isvc/{ISVC}"
    r = requests.put(url, data="\n".join(lines) + "\n", timeout=10)  # PUT: 이 그룹의 이전 값을 통째로 교체
    r.raise_for_status()
    print(f"pushgateway: {len(lines)} series -> {url}")


def log_mlflow(summary: dict, rows: int, report_uri: str) -> None:
    import mlflow

    mlflow.set_tracking_uri(MLFLOW_URI)
    mlflow.set_experiment("iris-drift-monitoring")
    with mlflow.start_run(run_name=f"drift-{datetime.now(timezone.utc):%Y%m%d-%H%M}"):
        mlflow.set_tags({"isvc": ISVC, "report_uri": report_uri})
        mlflow.log_params({"max_window_minutes": WINDOW_MINUTES, "drift_share_threshold": DRIFT_SHARE})
        mlflow.log_metrics({"current_rows": rows, "drift_share": summary["drift_share"],
                            "drifted_columns": summary["drifted_columns"],
                            "dataset_drift": int(summary["dataset_drift"]),
                            **{f"drift_score.{k}": v["score"] for k, v in summary["columns"].items()}})
    print(f"mlflow: logged run to experiment iris-drift-monitoring ({MLFLOW_URI})")


def make_report() -> int:
    from evidently import DataDefinition, Dataset, Report
    from evidently.presets import DataDriftPreset, DataSummaryPreset

    now = datetime.now(timezone.utc)
    ref = pd.read_csv(io.BytesIO(s3.get_object(Bucket=BUCKET, Key=f"{PREFIX}/reference/iris.csv")["Body"].read()))
    since = now - timedelta(minutes=WINDOW_MINUTES)
    state_key = f"{PREFIX}/reports/_state.json"
    try:
        last = datetime.fromisoformat(json.loads(s3.get_object(Bucket=BUCKET, Key=state_key)["Body"].read())["last_report_at"])
        since = max(since, last)  # 이미 리포트에 들어간 로그는 다시 보지 않는다
    except s3.exceptions.NoSuchKey:
        pass
    print(f"window: {since:%Y-%m-%d %H:%M:%S} ~ {now:%H:%M:%S} UTC")
    cur = load_current(since)
    if len(cur) < MIN_ROWS:
        # 상태를 갱신하지 않으므로 로그는 다음 회차에 이어서 쌓인다
        print(f"skip: 새 로그가 {len(cur)}행으로 MIN_ROWS={MIN_ROWS} 미만")
        return 0

    schema = DataDefinition(numerical_columns=FEATURES, categorical_columns=["prediction"])
    report = Report([DataDriftPreset(drift_share=DRIFT_SHARE), DataSummaryPreset()], include_tests=True)
    snapshot = report.run(current_data=Dataset.from_pandas(cur, data_definition=schema),
                          reference_data=Dataset.from_pandas(ref, data_definition=schema))

    summary = summarize(snapshot)
    summary.update({"isvc": ISVC, "generated_at": now.isoformat(), "window_start": since.isoformat(),
                    "reference_rows": len(ref), "current_rows": len(cur)})

    stamp = f"{now:%Y%m%dT%H%M%SZ}"
    html = snapshot.get_html_str(as_iframe=False).encode()
    base = f"{PREFIX}/reports/drift-{stamp}"
    s3.put_object(Bucket=BUCKET, Key=f"{base}.html", Body=html, ContentType="text/html")
    s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}/reports/latest.html", Body=html, ContentType="text/html")
    s3.put_object(Bucket=BUCKET, Key=f"{base}.json", Body=json.dumps(summary, indent=2).encode(),
                  ContentType="application/json")
    s3.put_object(Bucket=BUCKET, Key=state_key, Body=json.dumps({"last_report_at": now.isoformat()}).encode())
    report_uri = f"s3://{BUCKET}/{base}.html"
    print(f"report: {report_uri}")

    print(f"dataset_drift={summary['dataset_drift']} drift_share={summary['drift_share']:.2f} "
          f"({summary['drifted_columns']}/{len(summary['columns'])} columns)")
    for col, c in summary["columns"].items():
        print(f"  {col:<13} {c['method']:<20} score={c['score']:.4g} drift={c['drift']}")

    # 부가 출력은 실패해도 리포트 자체는 성공으로 둔다
    for name, enabled, fn in (("pushgateway", PUSHGATEWAY, push_metrics), ("mlflow", MLFLOW_URI, log_mlflow)):
        if not enabled:
            continue
        try:
            fn(summary, len(cur), *([report_uri] if name == "mlflow" else []))
        except Exception as e:
            print(f"{name} error: {e}")
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "report"
    if mode == "reference":
        make_reference()
    elif mode == "report":
        sys.exit(make_report())
    else:
        sys.exit(f"unknown mode: {mode}")
