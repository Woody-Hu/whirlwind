# Whirlwind runtime image (ADR-0005 D4).
# python:3.12-slim + `pip install .`; the repo itself is kept at /app because
# the echo image build installs `whirlwind @ file:///app` into every sandbox
# (imaging/base.py requirements), so repo_root must exist on disk inside the pod.

FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

# repo_root for image builds (whirlwind @ file:///app) + writable data dir on a volume
ENV PYTHONUNBUFFERED=1

EXPOSE 8410
VOLUME ["/data"]

ENTRYPOINT ["whirlwind"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8410", "--data-dir", "/data", "--repo-root", "/app"]
