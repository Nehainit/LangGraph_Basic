FROM python:3.13-slim

# ffmpeg/ffprobe do the final edit, audio mix, and video validation; DejaVu fonts draw captions and Edits text.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8001
# One worker: the LangGraph SQLite checkpointer and in-flight films live in this process.
CMD ["uvicorn", "video_automation.backend:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]
