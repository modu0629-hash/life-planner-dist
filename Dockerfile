FROM python:3.12-slim
# python 의존성 + Node.js + Claude Code CLI(자동입력 파싱용, Max 구독 토큰 사용)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && pip install --no-cache-dir flask requests pywebpush \
    && apt-get purge -y gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /root/.npm
WORKDIR /app
ENV TZ=Asia/Seoul
CMD ["python", "app.py"]
