FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COW_DATA_DIR=/tmp/cowagent-poc-data

RUN python -m pip install --no-cache-dir \
    playwright==1.49.1 \
    requests \
    chardet \
    pillow \
    python-dotenv \
    PyYAML \
    click \
    croniter \
    json-repair

WORKDIR /report
