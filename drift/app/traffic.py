"""iris-drift InferenceService 에 테스트 트래픽을 보낸다 (Mac 에서 포트포워딩 후 실행).

  python traffic.py --mode normal  --requests 30   # 학습 분포와 같은 입력
  python traffic.py --mode drift   --requests 30   # 꽃잎 크기를 키운 입력 (계절/산지 변화 같은 공변량 드리프트)
"""

import argparse

import numpy as np
import requests
from sklearn.datasets import load_iris


def make_batch(rng, X, mode: str, size: int) -> np.ndarray:
    batch = X[rng.integers(0, len(X), size)] + rng.normal(0, 0.1, (size, 4))  # 학습 샘플 + 작은 잡음
    if mode == "drift":
        batch[:, 2] = batch[:, 2] * 1.3 + 1.0   # petal length: 커지고 퍼진다
        batch[:, 3] = batch[:, 3] + 0.8         # petal width: 전체가 이동
    return np.clip(batch, 0.1, None).round(2)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8087/v1/models/iris-drift:predict")
    p.add_argument("--mode", choices=["normal", "drift"], default="normal")
    p.add_argument("--requests", type=int, default=30)
    p.add_argument("--batch", type=int, default=10, help="요청 하나에 담는 샘플 수")
    p.add_argument("--seed", type=int, default=0)
    a = p.parse_args()

    rng = np.random.default_rng(a.seed)
    X = load_iris().data
    counts = np.zeros(3, dtype=int)
    for _ in range(a.requests):
        r = requests.post(a.url, json={"instances": make_batch(rng, X, a.mode, a.batch).tolist()}, timeout=10)
        r.raise_for_status()
        counts += np.bincount(r.json()["predictions"], minlength=3)
    print(f"mode={a.mode} requests={a.requests} samples={a.requests * a.batch} "
          f"predictions(setosa/versicolor/virginica)={counts.tolist()}")


if __name__ == "__main__":
    main()
