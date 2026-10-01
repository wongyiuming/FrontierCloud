# Minimal, disposable database interoperability test runner.
FROM python:3.14.7-slim
WORKDIR /src
COPY pyproject.toml .
RUN python -c "import tomllib,subprocess; deps=tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']; names=('sqlalchemy==','asyncmy==','aiosqlite==','pydantic-settings==','cryptography=='); subprocess.check_call(['python','-m','pip','install','--no-cache-dir',*[d for d in deps if d.startswith(names)]])"
COPY app ./app
COPY migrations ./migrations
COPY protocol ./protocol
COPY tests/test_protocol_conformance.py tests/test_sqlite_store.py ./tests/
COPY tests/store_interop_smoke.py .
RUN chmod -R a+rX /src/app /src/migrations /src/protocol /src/tests && \
    chmod a+r /src/store_interop_smoke.py
ENV PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
ENTRYPOINT ["python", "/src/store_interop_smoke.py"]
