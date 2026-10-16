# Imagem do cron do Railway: so o job analyze-llm. NUNCA instale o extra `sentiment`
# (torch ~2 GB) -- a imagem passaria de 3 GB para rodar 1 chamada HTTP por dia.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY src ./src
COPY alembic ./alembic
RUN pip install --no-cache-dir ".[mysql,llm]"
CMD ["python", "-m", "tcc_scraper.cli", "analyze-llm"]
