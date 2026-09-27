# Works on Hugging Face Spaces (Docker), Fly.io, Railway, Koyeb, any container host.
FROM python:3.11-slim
WORKDIR /app
COPY . /app
ENV PYTHONUNBUFFERED=1 PORT=7860
EXPOSE 7860
CMD ["python", "server.py"]
