FROM python:3.12-slim AS runtime

# The package resolves its repo config relative to the installed module, which
# in a non-editable install points into site-packages, not /app. Without these
# two variables the image silently ignores the config/ copied below.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MARKET_SCANNER_OUTPUT_DIR=/app/AllScreenersResults \
    MARKET_SCANNER_CONFIG=/app/config/scanner.toml \
    MARKET_SCANNER_UNIVERSE=/app/config/universe.txt

RUN groupadd --system scanner \
    && useradd --system --gid scanner --create-home scanner

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY config/ ./config/
COPY src/ ./src/
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir .

RUN mkdir -p /app/AllScreenersResults && chown scanner:scanner /app/AllScreenersResults
USER scanner

ENTRYPOINT ["market-scanner"]
CMD ["scan", "--provider", "alpaca", "--output-dir", "/app/AllScreenersResults"]
