# BentoML 1.4 계열: 학습 없이 Iris 모델을 기동 시 학습하는 최소 서비스
import bentoml
from sklearn.datasets import load_iris
from sklearn.ensemble import RandomForestClassifier


@bentoml.service(resources={"cpu": "1"}, traffic={"timeout": 30})
class IrisClassifier:
    def __init__(self) -> None:
        X, y = load_iris(return_X_y=True)
        self.model = RandomForestClassifier(n_estimators=50, random_state=0).fit(X, y)

    @bentoml.api
    def predict(self, data: list[list[float]]) -> list[int]:
        return self.model.predict(data).tolist()
