# 파이프라인을 컴파일하고 KFP API 에 실행을 제출한 뒤 완료까지 기다린다.
# 필요한 환경변수: KFP_TOKEN (audience=pipelines.kubeflow.org 인 ServiceAccount 토큰)
import argparse
import os
import sys

import kfp
from kfp import compiler

from iris_pipeline import iris_pipeline

NS = os.environ.get("NS", "kubeflow-user-example-com")
HOST = os.environ.get("KFP_HOST", "http://localhost:8888")

p = argparse.ArgumentParser()
p.add_argument("--n-estimators", type=int, default=100)
p.add_argument("--accuracy-threshold", type=float, default=0.85)
p.add_argument("--isvc-name", default="iris-kfp")
p.add_argument("--experiment", default="iris-kserve")
p.add_argument("--timeout", type=int, default=1800)
args = p.parse_args()

compiler.Compiler().compile(iris_pipeline, "iris_pipeline.yaml")
client = kfp.Client(host=HOST, existing_token=os.environ["KFP_TOKEN"], namespace=NS)
run = client.create_run_from_pipeline_package(
    "iris_pipeline.yaml",
    arguments={
        "n_estimators": args.n_estimators,
        "accuracy_threshold": args.accuracy_threshold,
        "isvc_name": args.isvc_name,
    },
    run_name=f"iris-train-deploy-n{args.n_estimators}",
    experiment_name=args.experiment,
    namespace=NS,
    enable_caching=False,
)
print(f"run_id={run.run_id}")
result = client.wait_for_run_completion(run.run_id, timeout=args.timeout)
print(f"state={result.state}")
sys.exit(0 if result.state == "SUCCEEDED" else 1)
