#!/usr/bin/env bash
# Track A 실습: BentoML 빌드 → kind 적재 → KServe 배포 → 호출 (Mac에서 실행)
#
# 사용법: ./run-lab.sh <단계>
#   build       venv 준비, bentoml build/containerize, kind load
#   sklearn     KServe 내장 sklearn 런타임 (Serverless) 배포 + 호출
#   raw         Bento를 RawDeployment로 배포 + 호출
#   serverless  Bento를 Serverless(Knative, minReplicas 0)로 배포 + 호출
#   all         build → sklearn → raw → serverless
#   status      InferenceService / 파드 / PodAutoscaler 상태
#   clean       실습 InferenceService 삭제
#
# 환경변수로 바꿀 수 있는 값: CLUSTER, NS, IMAGE, BENTOML_VERSION
set -euo pipefail
cd "$(dirname "$0")"

CLUSTER=${CLUSTER:-myk8s}
NS=${NS:-kubeflow-user-example-com}
IMAGE=${IMAGE:-dev.local/iris-bento:v1}   # 매니페스트의 image 와 같아야 한다
BENTOML_VERSION=${BENTOML_VERSION:-1.4.40}

SKLEARN_DATA='{"instances": [[6.8,2.8,4.8,1.4],[6.0,3.4,4.5,1.6]]}'
BENTO_DATA='{"data": [[5.1,3.5,1.4,0.2],[6.7,3.0,5.2,2.3]]}'

PF_PID=""
stop_pf() { if [ -n "$PF_PID" ]; then kill "$PF_PID" 2>/dev/null || true; PF_PID=""; fi; }
trap stop_pf EXIT

step() { printf '\n=== %s\n' "$*"; }

# call <port-forward 대상> <로컬포트> <원격포트> <경로> <JSON>
# 배포 직후에는 port-forward 가 끊길 수 있어 최대 5회 다시 띄워 호출한다.
# 포트 대기는 연결을 만들지 않는 lsof 로 확인한다 (빈 연결은 port-forward 를 종료시킬 수 있다).
call() {
  local target=$1 lport=$2 rport=$3 path=$4 data=$5
  for attempt in 1 2 3 4 5; do
    kubectl -n "$NS" port-forward "$target" "$lport:$rport" >/dev/null 2>&1 &
    PF_PID=$!
    for _ in $(seq 1 20); do
      lsof -iTCP:"$lport" -sTCP:LISTEN -P -n >/dev/null 2>&1 && break
      sleep 0.5
    done
    if curl -sS --fail-with-body -X POST "localhost:${lport}${path}" \
         -H 'Content-Type: application/json' -d "$data"; then
      echo; stop_pf; return 0
    fi
    echo "  (호출 실패, 재시도 $attempt/5)" >&2
    stop_pf; sleep 3
  done
  return 1
}

# deploy <매니페스트> <isvc 이름>
deploy() {
  kubectl apply -f "$1"
  kubectl -n "$NS" wait --for=condition=Ready "isvc/$2" --timeout=300s
  kubectl -n "$NS" get isvc "$2"
}

# Serverless 는 최신 Ready Revision 의 Deployment 로 포트포워딩한다.
# (파드를 라벨로 고르면 삭제 중인 이전 파드가 잡힐 수 있다)
revision_deploy() {
  echo "deploy/$(kubectl -n "$NS" get isvc "$1" \
    -o jsonpath='{.status.components.predictor.latestReadyRevision}')-deployment"
}

setup_venv() {
  [ -d .venv ] || uv venv -q -p 3.11 .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  uv pip install -q "bentoml==${BENTOML_VERSION}" scikit-learn numpy
  bentoml --version
}

do_build() {
  step "build: BentoML ${BENTOML_VERSION} → ${IMAGE} → kind(${CLUSTER})"
  kind get clusters | grep -qx "$CLUSTER" || { echo "kind 클러스터 '$CLUSTER' 없음" >&2; exit 1; }
  setup_venv
  bentoml build
  # Apple Silicon 이므로 arm64 이미지가 만들어진다 (kind 노드도 arm64)
  bentoml containerize iris_classifier:latest -t "$IMAGE"
  kind load docker-image "$IMAGE" --name "$CLUSTER"
}

do_sklearn() {
  step "sklearn: KServe 내장 런타임 (Serverless)"
  deploy k8s/sklearn-iris.yaml sklearn-iris
  call "$(revision_deploy sklearn-iris)" 8085 8080 /v1/models/sklearn-iris:predict "$SKLEARN_DATA"
}

do_raw() {
  step "raw: Bento RawDeployment"
  deploy k8s/isvc-bento-raw.yaml iris-bento
  call svc/iris-bento-predictor 3000 80 /predict "$BENTO_DATA"
}

do_serverless() {
  step "serverless: Bento Serverless (minReplicas 0)"
  deploy k8s/isvc-bento-serverless.yaml iris-bento-sl
  # *-predictor 서비스는 게이트웨이를 거쳐 인증 정책에 막히므로 Deployment 로 직접 포트포워딩
  call "$(revision_deploy iris-bento-sl)" 3001 3000 /predict "$BENTO_DATA"
  echo "약 80초 뒤 0개로 줄어든다. 관찰: kubectl -n $NS get podautoscaler -w"
}

do_status() {
  kubectl -n "$NS" get isvc,pods
  kubectl -n "$NS" get podautoscaler 2>/dev/null || true
}

do_clean() {
  step "clean"
  kubectl -n "$NS" delete isvc sklearn-iris iris-bento iris-bento-sl --ignore-not-found
}

case "${1:-}" in
  build)      do_build ;;
  sklearn)    do_sklearn ;;
  raw)        do_raw ;;
  serverless) do_serverless ;;
  all)        do_build; do_sklearn; do_raw; do_serverless ;;
  status)     do_status ;;
  clean)      do_clean ;;
  *) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
