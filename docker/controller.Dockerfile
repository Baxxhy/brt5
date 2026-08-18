FROM python:3.11-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir \
        datasets==5.0.0 \
        packaging==26.0 \
        requests==2.34.2 \
        docker \
        unidiff \
        python-dotenv \
        tqdm \
        fire \
        editdistance \
        GitPython \
        pytest

WORKDIR /root/Baxxhy/BugReproduce
