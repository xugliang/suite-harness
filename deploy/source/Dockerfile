# Supply an approved Python image digest at build time.
ARG PYTHON_BASE
FROM ${PYTHON_BASE}
WORKDIR /srv/suite-harness
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/srv/suite-harness/src
COPY requirements /srv/suite-harness/requirements
RUN python -m pip install --no-cache-dir -r requirements/server.txt
COPY src /srv/suite-harness/src
COPY deploy/source /srv/suite-harness/deploy/source
USER 65532:65532
# The derived product image adds its source, dependencies and explicit config.
ENTRYPOINT ["python", "deploy/source/run.py"]
CMD ["/etc/suite-harness/deployment.yaml"]
