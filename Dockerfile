FROM python:3.14-alpine

# Non-root user for defense-in-depth when running as a container.
RUN addgroup -S ascode && adduser -S ascode -G ascode
WORKDIR /app

# Install deps first (layer caching).
COPY pyproject.toml README.md ./
COPY litellm_as_code ./litellm_as_code
RUN pip install --no-cache-dir .

# The mounted spec lives outside the image.
ENV LITELLM_SPEC=/config/spec.yml

USER ascode
ENTRYPOINT ["litellm-as-code"]
