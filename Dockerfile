FROM python:3.12-slim
WORKDIR /app
COPY app.py lunardate.py chat.html ./
COPY command-center ./command-center
COPY principle ./principle
COPY dispatch ./dispatch
COPY data.sample.json notes.sample.json job-plan.sample.json ./
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && sed -i 's/ThreadingHTTPServer(("127.0.0.1", port)/ThreadingHTTPServer(("0.0.0.0", port)/' app.py && grep -n "0.0.0.0" app.py
ENV WB_CHAT_DIR=/data/chat WB_BUS_DIR=/data/agent-bus
EXPOSE 18080
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "app.py", "18080"]
