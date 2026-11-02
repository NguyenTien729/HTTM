# syntax=docker/dockerfile:1

FROM python:3.12-slim

WORKDIR /code

# Cài thư viện chính
COPY requirements.txt ./

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefer-binary -r requirements.txt

# Cài thư viện test/dev
COPY requirements-dev.txt ./

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefer-binary -r requirements-dev.txt

# Copy source code sau cùng
COPY app ./app
COPY tests ./tests
COPY pytest.ini ./

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]