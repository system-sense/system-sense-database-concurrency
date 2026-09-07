FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 9100
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9100", "--log-level", "warning"]
