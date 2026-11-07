# KFP v2 파이프라인: Iris 학습 → 평가 → (정확도 기준 통과 시) KServe 배포 → 스모크 테스트
# 모델은 KFP 아티팩트로 SeaweedFS(minio-service)에 저장되고, KServe 가 그 경로(s3://)에서 직접 받아 서빙한다.
from kfp import compiler, dsl, kubernetes
from kfp.dsl import Dataset, Input, Metrics, Model, Output

BASE_IMAGE = "python:3.11-slim"
# KServe sklearnserver v0.15.2 와 같은 버전으로 학습해야 pickle 호환이 보장된다
SKLEARN = ["scikit-learn==1.5.2", "numpy==2.0.2", "joblib==1.4.2"]


@dsl.component(base_image=BASE_IMAGE, packages_to_install=SKLEARN + ["pandas==2.2.3"])
def load_data(train: Output[Dataset], test: Output[Dataset], test_size: float = 0.2, seed: int = 42):
    from sklearn.datasets import load_iris
    from sklearn.model_selection import train_test_split

    df = load_iris(as_frame=True).frame  # 특성 4개 + target(0/1/2)
    tr, te = train_test_split(df, test_size=test_size, random_state=seed, stratify=df["target"])
    tr.to_csv(train.path, index=False)
    te.to_csv(test.path, index=False)
    train.metadata["rows"] = len(tr)
    test.metadata["rows"] = len(te)


@dsl.component(base_image=BASE_IMAGE, packages_to_install=SKLEARN + ["pandas==2.2.3"])
def train(train: Input[Dataset], model: Output[Model], n_estimators: int = 100, seed: int = 42):
    import os

    import joblib
    import pandas as pd
    from sklearn.ensemble import RandomForestClassifier

    df = pd.read_csv(train.path)
    clf = RandomForestClassifier(n_estimators=n_estimators, random_state=seed)
    clf.fit(df.drop(columns="target").values, df["target"].values)

    # KServe sklearn 런타임은 디렉터리 안의 model.joblib 을 읽는다
    os.makedirs(model.path, exist_ok=True)
    joblib.dump(clf, os.path.join(model.path, "model.joblib"))
    model.metadata["framework"] = "sklearn"
    model.metadata["n_estimators"] = n_estimators


@dsl.component(base_image=BASE_IMAGE, packages_to_install=SKLEARN + ["pandas==2.2.3"])
def evaluate(test: Input[Dataset], model: Input[Model], metrics: Output[Metrics]) -> float:
    import os

    import joblib
    import pandas as pd
    from sklearn.metrics import accuracy_score

    df = pd.read_csv(test.path)
    clf = joblib.load(os.path.join(model.path, "model.joblib"))
    acc = float(accuracy_score(df["target"].values, clf.predict(df.drop(columns="target").values)))
    metrics.log_metric("accuracy", acc)
    print(f"accuracy={acc:.4f}")
    return acc


@dsl.component(base_image=BASE_IMAGE, packages_to_install=["kubernetes==31.0.0"])
def deploy(model: Input[Model], isvc_name: str, timeout_sec: int = 600) -> str:
    """KServe 용 S3 자격증명(Secret/SA)을 준비하고 InferenceService 를 생성 또는 갱신한다."""
    import time

    from kubernetes import client, config
    from kubernetes.client.rest import ApiException

    config.load_incluster_config()
    ns = open("/var/run/secrets/kubernetes.io/serviceaccount/namespace").read().strip()
    core, crd = client.CoreV1Api(), client.CustomObjectsApi()

    # 1) KFP 가 쓰는 SeaweedFS 자격증명을 KServe 형식(AWS_* 키 + s3 어노테이션)으로 옮긴다.
    #    값은 클러스터 안에서만 복사되고 로그에 남기지 않는다.
    src = core.read_namespaced_secret("mlpipeline-minio-artifact", ns).data
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(name="kserve-s3-creds", annotations={
            "serving.kserve.io/s3-endpoint": "minio-service.kubeflow:9000",
            "serving.kserve.io/s3-usehttps": "0",
            "serving.kserve.io/s3-region": "us-east-1",
        }),
        type="Opaque",
        data={"AWS_ACCESS_KEY_ID": src["accesskey"], "AWS_SECRET_ACCESS_KEY": src["secretkey"]},
    )
    sa = client.V1ServiceAccount(metadata=client.V1ObjectMeta(name="kserve-s3-sa"),
                                 secrets=[client.V1ObjectReference(name="kserve-s3-creds")])
    for create, replace, body in (
        (core.create_namespaced_secret, core.replace_namespaced_secret, secret),
        (core.create_namespaced_service_account, core.replace_namespaced_service_account, sa),
    ):
        try:
            create(ns, body)
        except ApiException as e:
            if e.status != 409:
                raise
            replace(body.metadata.name, ns, body)

    # 2) KFP 아티팩트 URI(minio://bucket/key) → KServe storageUri(s3://bucket/key)
    storage_uri = model.uri.replace("minio://", "s3://", 1)
    print("storageUri:", storage_uri)

    group, ver, plural = "serving.kserve.io", "v1beta1", "inferenceservices"
    spec = {"predictor": {
        "serviceAccountName": "kserve-s3-sa",
        "minReplicas": 1,
        "model": {
            "modelFormat": {"name": "sklearn"},
            "storageUri": storage_uri,
            "resources": {"requests": {"cpu": "100m", "memory": "256Mi"},
                          "limits": {"cpu": "1", "memory": "1Gi"}},
        },
    }}

    prev_rev = None
    try:
        cur = crd.get_namespaced_custom_object(group, ver, ns, plural, isvc_name)
        prev_rev = cur.get("status", {}).get("components", {}).get("predictor", {}).get("latestReadyRevision")
        crd.patch_namespaced_custom_object(group, ver, ns, plural, isvc_name, {"spec": spec})
        print(f"patched isvc/{isvc_name} (이전 revision: {prev_rev})")
    except ApiException as e:
        if e.status != 404:
            raise
        crd.create_namespaced_custom_object(group, ver, ns, plural, {
            "apiVersion": f"{group}/{ver}", "kind": "InferenceService",
            "metadata": {"name": isvc_name, "labels": {"app": "iris-kfp"}},
            "spec": spec,
        })
        print(f"created isvc/{isvc_name}")

    # 3) 새 revision 이 Ready 가 될 때까지 대기 (갱신이면 revision 이름이 바뀌어야 한다)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        st = crd.get_namespaced_custom_object(group, ver, ns, plural, isvc_name).get("status", {})
        ready = any(c["type"] == "Ready" and c["status"] == "True" for c in st.get("conditions", []))
        rev = st.get("components", {}).get("predictor", {}).get("latestReadyRevision")
        if ready and rev and rev != prev_rev:
            print(f"Ready: revision={rev} url={st.get('url')}")
            return rev
        time.sleep(10)
    raise TimeoutError(f"isvc/{isvc_name} 가 {timeout_sec}초 안에 Ready 가 되지 않았다")


@dsl.component(base_image=BASE_IMAGE, packages_to_install=["requests==2.32.3"])
def smoke_test(isvc_name: str, revision: str):
    """revision 의 private 서비스(queue-proxy)로 직접 예측을 요청해 응답을 확인한다.

    Kubeflow 프로필 네임스페이스는 '같은 네임스페이스의 메시 워크로드'만 허용하므로,
    이 컴포넌트 파드에는 파이프라인 정의에서 Istio 사이드카를 붙인다 (KFP 기본값은 사이드카 없음).
    """
    import requests

    ns = open("/var/run/secrets/kubernetes.io/serviceaccount/namespace").read().strip()
    url = f"http://{revision}-private.{ns}.svc.cluster.local/v1/models/{isvc_name}:predict"
    body = {"instances": [[5.1, 3.5, 1.4, 0.2], [6.0, 3.4, 4.5, 1.6], [6.7, 3.0, 5.2, 2.3]]}
    r = requests.post(url, json=body, timeout=30)
    print(r.status_code, r.text)
    r.raise_for_status()
    preds = r.json()["predictions"]
    assert preds == [0, 1, 2], f"예상과 다른 예측: {preds}"


@dsl.pipeline(name="iris-train-deploy", description="Iris 학습 → 평가 → KServe 배포")
def iris_pipeline(n_estimators: int = 100, accuracy_threshold: float = 0.85, isvc_name: str = "iris-kfp"):
    data = load_data()
    trained = train(train=data.outputs["train"], n_estimators=n_estimators)
    evaluated = evaluate(test=data.outputs["test"], model=trained.outputs["model"])

    with dsl.If(evaluated.outputs["Output"] >= accuracy_threshold, name="accuracy-gate"):
        deployed = deploy(model=trained.outputs["model"], isvc_name=isvc_name)
        deployed.set_caching_options(False)
        tested = smoke_test(isvc_name=isvc_name, revision=deployed.output)
        tested.set_caching_options(False)
        # 라벨은 KFP 가 붙이는 sidecar.istio.io/inject=false 어노테이션보다 우선한다
        kubernetes.add_pod_label(tested, "sidecar.istio.io/inject", "true")


if __name__ == "__main__":
    compiler.Compiler().compile(iris_pipeline, "iris_pipeline.yaml")
    print("compiled: iris_pipeline.yaml")
