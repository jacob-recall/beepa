FROM python:3.11-slim
COPY requirements-notifications.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --index-url https://pypi.org/simple -r /tmp/requirements.txt
COPY notifications.py /app/notifications.py
ENTRYPOINT ["python3", "/app/notifications.py", "--helper"]
