# Disposable database interoperability and real service regression test runner.
FROM python:3.14.7-slim
WORKDIR /src
COPY pyproject.toml .
# The SQLite suite now imports the real API promotion/security paths, including
# Redis, Starlette, streaming ZIP and metrics. Use the complete pinned project
# dependency graph instead of a stale whitelist that fails before tests run.
RUN python -c "import tomllib,subprocess; deps=tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']; subprocess.check_call(['python','-m','pip','install','--no-cache-dir',*deps])"
COPY app ./app
COPY migrations ./migrations
COPY protocol ./protocol
COPY tests/test_protocol_conformance.py tests/test_sqlite_store.py tests/test_capability_vectors.py tests/test_release_manifest_vectors.py ./tests/
COPY tests/store_interop_smoke.py .
RUN chmod -R a+rX /src/app /src/migrations /src/protocol /src/tests && \
    chmod a+r /src/store_interop_smoke.py && \
    mkdir -p /src/data && chown 10001:10001 /src/data
ENV PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
ENTRYPOINT ["python", "/src/store_interop_smoke.py"]
