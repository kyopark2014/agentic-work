FROM --platform=linux/arm64 python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    nodejs \
    npm \
    # myslide: cairosvg / SVG→PNG (runtime + compile deps for cairocffi on slim)
    build-essential \
    pkg-config \
    libffi-dev \
    libcairo2 \
    libcairo2-dev \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    shared-mime-info \
    librsvg2-bin \
    && rm -rf /var/lib/apt/lists/*

RUN pip install boto3 botocore --upgrade
RUN pip install langchain_aws langchain langchain_community langgraph langchain-openai langchain-anthropic "openai>=2.41.0" aws-bedrock-token-generator
RUN pip install langgraph-checkpoint-sqlite aiosqlite
# Keep FastMCP (mcp.server.fastmcp). mcp 2.x removed it; pin 1.x for stdio MCP servers.
RUN pip install "mcp>=1.9.0,<2" langchain-mcp-adapters awslabs.aws-documentation-mcp-server
RUN pip install bedrock-agentcore uv
RUN pip install "mcp>=1.9.0,<2"
RUN pip install Pillow pytz tavily-python langchain-text-splitters
RUN pip install requests beautifulsoup4
RUN pip install python-docx openpyxl reportlab matplotlib pandas finance-datareader pypdf pdfplumber pyyaml defusedxml lxml
RUN pip install colorama rich
# myslide / pptx QA: PPTX edit + SVG→PNG (avoid runtime pip install delays)
RUN pip install python-pptx cairosvg

# Skills that use docx-js / pptxgenjs / sharp via bash/node (local install for require())
RUN npm install docx pptxgenjs sharp
ENV NODE_PATH=/app/node_modules

# OpenTelemetry (ADOT + LangChain instrumentation for AgentCore Evaluations)
RUN pip install aws-opentelemetry-distro>=0.10.0
RUN pip install opentelemetry-instrumentation-langchain

COPY . .

# Add the current directory to Python path
ENV PYTHONPATH=/app
# Minimal config (MCP/LangGraph agent without full RAG keys in config.json)
ENV ALLOW_MISSING_RAG_CONFIG=1
ENV SESSION_STORAGE_DIR=/mnt/workspace
ENV OTEL_PYTHON_DISTRO=aws_distro
ENV OTEL_PYTHON_CONFIGURATOR=aws_configurator
ENV OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true
ARG OTEL_SERVICE_NAME=agentic_work.DEFAULT
ENV OTEL_RESOURCE_ATTRIBUTES=service.name=${OTEL_SERVICE_NAME}

EXPOSE 8080

CMD ["uv", "run", "opentelemetry-instrument", "uvicorn", "agent:app", "--host", "0.0.0.0", "--port", "8080"]
