FROM python:3.7-slim

RUN apt-get update && apt-get install -y gcc git libpq-dev

RUN pip install --prefix="/install" --no-warn-script-location cryptography

COPY . /sygnal/
WORKDIR /sygnal/

RUN pip install --no-warn-script-location cryptography
RUN pip install --no-warn-script-location -r requirements.txt

EXPOSE 5000/tcp

ENTRYPOINT ["python", "-m", "sygnal.sygnal"]
