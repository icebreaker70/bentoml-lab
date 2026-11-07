#!/usr/bin/env bash
# Mac(zsh/bash)에서 실행: ./check-env.sh
# Colima / kind / K8s 1.34 / Kubeflow 1.11 / KServe 상태 점검
set -u
ok(){ printf "  [OK]   %s\n" "$*"; }
warn(){ printf "  [WARN] %s\n" "$*"; }
bad(){ printf "  [FAIL] %s\n" "$*"; }

echo "== 1. CLI 도구"
for c in colima docker kind kubectl kustomize python3 uv; do
  if command -v $c >/dev/null; then ok "$c: $($c version --short 2>/dev/null | head -1 || true) $(command -v $c)"; else warn "$c 없음"; fi
done
kustomize version 2>/dev/null | grep -q 'v5.7' && ok "kustomize 5.7.x" || warn "kustomize는 5.7.1 권장"

echo "== 2. Colima / Docker 리소스"
colima status 2>&1 | head -3
docker info --format 'CPU={{.NCPU}}  MEM={{.MemTotal}}  Arch={{.Architecture}}' 2>&1
echo "  (Kubeflow 권장: CPU 16 / RAM 32GB. 최소 CPU 8 / RAM 16GB 이상 권장)"
colima list 2>&1 | head -3

echo "== 3. kind 클러스터"
kind get clusters
kubectl config current-context
kubectl get nodes -o wide
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"  "}{.status.nodeInfo.kubeletVersion}{"  "}{.status.nodeInfo.architecture}{"\n"}{end}'

echo "== 4. Kubeflow / KServe 파드 (Running/Completed 아닌 것)"
kubectl get pods -A --no-headers 2>/dev/null | grep -vE 'Running|Completed' || ok "비정상 파드 없음"
echo "-- 핵심 네임스페이스"
for ns in istio-system cert-manager auth knative-serving kubeflow kserve kubeflow-user-example-com; do
  printf "  %-26s" $ns; kubectl get pods -n $ns --no-headers 2>/dev/null | wc -l | tr -d ' '; done

echo "== 5. KServe 설정"
kubectl get crd inferenceservices.serving.kserve.io -o name 2>&1
kubectl -n kubeflow get deploy kserve-controller-manager -o jsonpath='{.spec.template.spec.containers[*].image}{"\n"}' 2>&1
kubectl -n kubeflow get cm inferenceservice-config -o jsonpath='{.data.deploy}{"\n"}' 2>&1
echo "-- 사용자 네임스페이스 / PSS"
kubectl get ns kubeflow-user-example-com --show-labels 2>&1 | cut -c1-220
kubectl get ns kubeflow --show-labels 2>&1 | grep -o 'pod-security[^,]*'

echo "== 6. Gateway / 포트"
kubectl -n istio-system get svc istio-ingressgateway 2>&1
lsof -iTCP:8080 -sTCP:LISTEN -P -n 2>/dev/null | head -2 || true
