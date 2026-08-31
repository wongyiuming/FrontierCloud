FROM python:3.13-alpine

RUN addgroup -S -g 10002 reporter && adduser -S -G reporter -u 10002 reporter
WORKDIR /opt/frontiercloud-reporting
COPY monitoring/reporting/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY monitoring/reporting/weekly_report.py ./weekly_report.py
RUN install -d -o reporter -g reporter -m 0700 /var/lib/frontiercloud-report
USER 10002:10002
ENTRYPOINT ["python", "weekly_report.py"]
