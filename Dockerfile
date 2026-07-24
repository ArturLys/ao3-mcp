# Minimal image so registries like Glama can start the server and introspect its
# tools. The server boots WITHOUT an API key (only read_works needs one); provide
# GEMINI_API_KEY at runtime for actual fic reading:
#   docker run -i -e GEMINI_API_KEY=your-key ao3-mcp
FROM python:3.11-slim
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir .
ENTRYPOINT ["ao3-mcp"]
