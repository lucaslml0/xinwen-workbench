#!/bin/sh
set -e
mkdir -p /data/chat /data/agent-bus /data/reviews
cp /app/app.py /app/lunardate.py /app/chat.html /data/
mkdir -p /data/command-center /data/principle /data/dispatch /data/board
cp -a /app/command-center/. /data/command-center/
cp -a /app/principle/. /data/principle/
cp -a /app/dispatch/. /data/dispatch/
cp -a /app/board/. /data/board/
if [ ! -f /data/data.json ]; then cp /app/data.sample.json /data/data.json; fi
if [ ! -f /data/notes.json ]; then cp /app/notes.sample.json /data/notes.json; fi
if [ ! -f /data/job-plan.json ]; then cp /app/job-plan.sample.json /data/job-plan.json; fi
exec python3 /data/app.py 18080
