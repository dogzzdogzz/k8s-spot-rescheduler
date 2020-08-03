FROM python:3.8-slim

ENV PYTHONUNBUFFERED 1
WORKDIR /root
COPY requirements.txt /root/
RUN pip install -r /root/requirements.txt
COPY main.py kubernetes_units.txt /root/
CMD ["python", "main.py"]