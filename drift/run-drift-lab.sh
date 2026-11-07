#!/usr/bin/env bash
# 추론 로그 수집(KServe logger → inference-logger → SeaweedFS) + Evidently 드리프트 CronJob 실습
#
# 사용법: ./run-drift-lab.sh <단계> [인자]
#   build             이미지 빌드(dev.local/drift-lab:v1) → kind(myk8s) 적재
#   deploy            설정, 수신기, 기준 데이터, iris-drift isvc, CronJob 배포
#   traffic <mode> [N] normal|drift 트래픽 N개 요청(기본 30, 요청당 10샘플) 전송
#   report            CronJob 을 기다리지 않고 지금 한 번 실행하고 로그 출력
#   view              리포트 뷰어 포트포워딩 (http://localhost:8090/reports)
#   status            리소스 상태와 최근 Job
#   all               build → deploy → traffic normal → report → traffic drift → report
#   clean             실습 리소스 삭제 (S3 의 로그/리포트는 남긴다)
#
# 환경변수: CLUSTER(myk8s), NS(kubeflow-user-example-com), IMAGE(dev.local/drift-lab:v1)
set -euo pipefail
cd "$(dirname "$0")"

CLUSTER=${CLUSTER:-myk8s}
NS=${NS:-kubeflow-user-example-com}
IMAGE=${IMAGE:-dev.local/drift-lab:v1}
ISVC=iris-drift
TRAFFIC_PORT=8087
VIEW_PORT=8090

PF_PID=""
# wait 로 회수해야 bash 가 "Terminated: 15" 메시지를 찍지 않는다
stop_pf() { if [ -n "$PF_PID" ]; then kill "$PF_PID" 2>/dev/null || true; wait "$PF_PID" 2>/dev/null || true; PF_PID=""; fi; }
trap stop_pf EXIT

wait_port() {
  for _ in $(seq 1 20); do
    lsof -iTCP:"$1" -sTCP:LISTEN -P -n >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  echo "port-forward 실패 (port $1)" >&2; return 1
}

venv() {
  [ -d .venv ] || uv venv -q -p 3.11 .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  uv pip install -q numpy requests scikit-learn
}

build() {
  echo "=== build: $IMAGE → kind($CLUSTER)"
  docker build -q -t "$IMAGE" .
  kind load docker-image "$IMAGE" --name "$CLUSTER"
}

deploy() {
  echo "=== deploy: 설정 + 수신기"
  kubectl apply -f k8s/config.yaml -f k8s/inference-logger.yaml
  kubectl -n "$NS" rollout status deploy/inference-logger --timeout=180s

  echo "=== deploy: 기준 데이터 업로드 Job"
  kubectl -n "$NS" delete job drift-reference --ignore-not-found >/dev/null
  kubectl apply -f k8s/drift-reference-job.yaml
  kubectl -n "$NS" wait --for=condition=complete job/drift-reference --timeout=180s
  kubectl -n "$NS" logs job/drift-reference

  echo "=== deploy: InferenceService(logger 포함) + CronJob"
  kubectl apply -f k8s/isvc-iris-drift.yaml -f k8s/drift-cronjob.yaml
  kubectl -n "$NS" wait --for=condition=Ready "isvc/$ISVC" --timeout=300s
  kubectl -n "$NS" get isvc "$ISVC"
  kubectl -n "$NS" get cronjob drift-report
}

traffic() {
  local mode=${1:-normal} n=${2:-30} rev
  venv
  rev=$(kubectl -n "$NS" get isvc "$ISVC" -o jsonpath='{.status.components.predictor.latestReadyRevision}')
  # 8080(모델 서버)으로 바로 가면 logger 를 건너뛴다. agent 포트(9081)로 보내야 로그가 남는다.
  kubectl -n "$NS" port-forward "deploy/${rev}-deployment" "${TRAFFIC_PORT}:9081" >/dev/null 2>&1 &
  PF_PID=$!
  wait_port "$TRAFFIC_PORT"
  echo "=== traffic: $mode x $n (revision $rev)"
  python app/traffic.py --url "http://localhost:${TRAFFIC_PORT}/v1/models/${ISVC}:predict" --mode "$mode" --requests "$n"
  stop_pf
}

report() {
  local job
  job="drift-report-manual-$(date +%s)"
  echo "=== report: $job (logger 버퍼가 S3 로 flush 되도록 35초 대기)"
  sleep 35
  kubectl -n "$NS" create job "$job" --from=cronjob/drift-report >/dev/null
  kubectl -n "$NS" wait --for=condition=complete "job/$job" --timeout=300s || true
  kubectl -n "$NS" logs "job/$job"
}

view() {
  echo "=== view: http://localhost:${VIEW_PORT}/reports  (Ctrl+C 로 종료)"
  kubectl -n "$NS" port-forward svc/inference-logger "${VIEW_PORT}:80"
}

status() {
  kubectl -n "$NS" get isvc "$ISVC" 2>/dev/null || true
  kubectl -n "$NS" get deploy,svc -l app=inference-logger
  kubectl -n "$NS" get cronjob drift-report 2>/dev/null || true
  kubectl -n "$NS" get jobs --sort-by=.metadata.creationTimestamp | grep -E '^NAME|drift-' | tail -6 || true
}

clean() {
  kubectl -n "$NS" delete cronjob drift-report --ignore-not-found
  kubectl -n "$NS" get jobs -o name | grep -E 'job.batch/drift-' | xargs -r kubectl -n "$NS" delete
  kubectl -n "$NS" delete isvc "$ISVC" --ignore-not-found
  kubectl delete -f k8s/inference-logger.yaml -f k8s/config.yaml --ignore-not-found
}

case "${1:-}" in
  build) build ;;
  deploy) deploy ;;
  traffic) shift; traffic "$@" ;;
  report) report ;;
  view) view ;;
  status) status ;;
  all) build; deploy; traffic normal 30; report; traffic drift 30; report ;;
  clean) clean ;;
  *) sed -n '2,15p' "$0"; exit 1 ;;
esac
