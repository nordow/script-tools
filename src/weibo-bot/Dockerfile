FROM python:3.11.5-slim-bookworm

ENV TZ="Asia/Shanghai"
ENV CHROME_VERSION="126.0.6478.126"

WORKDIR /app

COPY . .

RUN apt-get -y update && apt-get -y install wget unzip
RUN bash get-chrome.sh && bash get-chromedriver.sh

RUN pip install -r requirements.txt

ENTRYPOINT ["python", "bot.py"]
