FROM nvidia/cuda:12.3.2-cudnn9-runtime-ubuntu22.04
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt /srv/requirements.txt
RUN pip3 install --no-cache-dir -r /srv/requirements.txt
COPY app /srv/app
WORKDIR /srv/app
ENV DATA_DIR=/data
EXPOSE 8099
CMD ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8099"]
