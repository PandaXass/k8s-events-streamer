FROM python:3.6-alpine

RUN apk update && apk upgrade \
    && apk add --no-cache --virtual .tmp-packeges gcc build-base openssl-dev libffi-dev \
    && pip install dumb-init==1.2.1\
    && apk del .tmp-packeges

RUN mkdir /app
COPY requirements.txt /app/
COPY k8s-events-streamer.py /app/
RUN pip install -r /app/requirements.txt

WORKDIR /app
ENTRYPOINT ["/usr/local/bin/dumb-init", "--"]
CMD ["python3", "./k8s-events-streamer.py"]
