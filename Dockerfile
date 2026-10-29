FROM python:3.12-slim
WORKDIR /code
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements.txt
COPY app ./app
COPY requirements-dev.txt ./
RUN pip install -r requirements-dev.txt
COPY tests ./tests
COPY pytest.ini ./
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
