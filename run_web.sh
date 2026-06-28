#!/usr/bin/sh
# LLMDirector Python Web Run Script
# source venv/bin/activate  # Configure your virtual environment path
cd web

# reset folder
rm -rf ~/.llmdirector

# kill existing
# kill existing
if fuser -s 8081/tcp; then
  fuser -k 8081/tcp >/dev/null 2>&1
fi

# Serving on port 8081 as per FSD §14
nohup python3 app.py --config ../LLMDirector.json >/dev/null 2>&1&


# to kill it:
# lsof -ti :8081 | xargs kill
deactivate
