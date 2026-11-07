#!/usr/bin/env bash
# KFP 파이프라인(학습 → 평가 → KServe 배포 → 스모크 테스트)을 Mac에서 제출하고, 끝나면 직접 호출해 본다.
#
# 사용법: ./run-pipeline.sh [run_pipeline.py 인자...]
#   예) ./run-pipeline.sh                         # 기본값 n_estimators=100, accuracy_threshold=0.85
#       ./run-pipeline.sh --n-estimators 10       # 다시 실행하면 같은 isvc 가 새 revision 으로 갱신된다
#       ./run-pipeline.sh --accuracy-threshold 1.01   # 기준 미달 → 배포 단계를 건너뛴다
#   ./run-pipeline.sh clean                       # isvc/iris-kfp 삭제
#
# 환경변수: NS, KFP_SDK_VERSION, ISVC
set -euo pipefail
cd "$(dirname "$0")"

NS=${NS:-kubeflow-user-example-com}
KFP_SDK_VERSION=${KFP_SDK_VERSION:-2.15.2}   # 서버(KFP 2.15.0)와 맞춘다
ISVC=${ISVC:-iris-kfp}

PF_PID=""
stop_pf() { if [ -n "$PF_PID" ]; then kill "$PF_PID" 2>/dev/null || true; PF_PID=""; fi; }
trap stop_pf EXIT

wait_port() {
  for _ in $(seq 1 20); do
    lsof -iTCP:"$1" -sTCP:LISTEN -P -n >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  echo "port-forward 실패 (port $1)" >&2; return 1
}

if [ "${1:-}" = "clean" ]; then
  kubectl -n "$NS" delete isvc "$ISVC" --ignore-not-found
  exit 0
fi

echo "=== 1. KFP SDK ${KFP_SDK_VERSION}"
[ -d .venv ] || uv venv -q -p 3.11 .venv
# shellcheck disable=SC1091
source .venv/bin/activate
uv pip install -q "kfp==${KFP_SDK_VERSION}" "kfp-kubernetes==${KFP_SDK_VERSION}"

echo "=== 2. 파이프라인 제출 (KFP API: svc/ml-pipeline 포트포워딩 + ServiceAccount 토큰)"
kubectl -n kubeflow port-forward svc/ml-pipeline 8888:8888 >/dev/null 2>&1 &
PF_PID=$!
wait_port 8888
KFP_TOKEN=$(kubectl -n "$NS" create token default-editor --audience=pipelines.kubeflow.org --duration=1h)
export KFP_TOKEN NS
python -W ignore::FutureWarning run_pipeline.py --isvc-name "$ISVC" "$@" || { echo "파이프라인 실패" >&2; exit 1; }
stop_pf

echo "=== 3. 배포 결과 확인"
if ! kubectl -n "$NS" get isvc "$ISVC" >/dev/null 2>&1; then
  echo "isvc/$ISVC 없음 (정확도 기준 미달로 배포를 건너뛰었을 수 있다)"; exit 0
fi
kubectl -n "$NS" get isvc "$ISVC"
REV=$(kubectl -n "$NS" get isvc "$ISVC" -o jsonpath='{.status.components.predictor.latestReadyRevision}')
kubectl -n "$NS" port-forward "deploy/${REV}-deployment" 8086:8080 >/dev/null 2>&1 &
PF_PID=$!
wait_port 8086
curl -sS --retry 3 --retry-all-errors -H 'Content-Type: application/json' \
  "localhost:8086/v1/models/${ISVC}:predict" \
  -d '{"instances": [[5.1,3.5,1.4,0.2],[6.0,3.4,4.5,1.6],[6.7,3.0,5.2,2.3]]}'
echo
