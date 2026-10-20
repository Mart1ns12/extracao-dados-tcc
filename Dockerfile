# Imagem do executor: worker sempre ligado no Railway (`python -m tcc_scraper.cli executor`).
# torch vem da roda CPU do PyTorch: a wheel padrao do PyPI traz CUDA (~2 GB) que o worker nunca usa.
# O torch CPU entra ANTES do projeto para o `pip install .` aceitar a versao ja instalada.
# Os modelos do FinBERT (pt/en) sao baixados no BUILD, para a subida do servico nao baixar ~900 MB
# do Hugging Face a cada deploy. HF_CACHE_DIR fixa o cache dentro da imagem (o padrao,
# PROJECT_ROOT/.hf_cache, cairia em /usr/local/lib/python3.12 depois do `pip install .`).
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 HF_HUB_DISABLE_TELEMETRY=1 \
    HF_CACHE_DIR=/app/.hf_cache
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir ".[llm,sentiment]"
RUN python -c "from tcc_scraper.sentiment.finbert import finbert_pt, finbert_en; finbert_pt()._load(); finbert_en()._load()"
CMD ["python", "-m", "tcc_scraper.cli", "executor"]
