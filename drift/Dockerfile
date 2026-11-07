# inference-logger(수신기)와 drift-report(CronJob)가 함께 쓰는 이미지
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/logger_server.py app/drift_job.py ./
# mlflow 가 uid → 사용자 이름을 조회하므로 실제 사용자를 만든다. git 이 없다는 경고도 끈다.
RUN useradd -u 1000 -m app
USER 1000
ENV PYTHONUNBUFFERED=1 GIT_PYTHON_REFRESH=quiet
CMD ["python", "logger_server.py"]
