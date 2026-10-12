# Use a patch-pinned official Debian-based Python 3.14 slim image.
FROM python:3.14.7-slim

# Set the application working directory.
WORKDIR /app

# Install locked dependencies before copying application sources. Code and test
# changes must not invalidate the dependency layer on every deployment.
# Keep builds usable with both the classic builder and BuildKit.
# Remove the ACME wheel's bundled test keys in this same installation layer;
# deleting them in a later layer would leave them in exported image history.
COPY pyproject.toml .
RUN chmod 0644 pyproject.toml && \
    python -c "import pathlib,tomllib; data=tomllib.loads(pathlib.Path('pyproject.toml').read_text()); pathlib.Path('/tmp/requirements.txt').write_text('\\n'.join(data['project']['dependencies'])+'\\n')" && \
    python -m pip install --no-cache-dir -r /tmp/requirements.txt && \
    python -c "import pathlib,shutil,sysconfig; site=pathlib.Path(sysconfig.get_path('purelib')).resolve(); target=site/'acme'/'_internal'/'tests'; target.resolve().relative_to(site); assert target.resolve() == target, 'ACME test directory escaped installed package'; assert not target.exists() or target.is_dir(), 'Unexpected ACME test fixture path'; shutil.rmtree(target) if target.is_dir() else None"

# Git worktree permissions can be restrictive on the host. Copy mutable sources
# after dependencies, then normalize them for the fixed unprivileged UID.
COPY main.py .
COPY app ./app
COPY static ./static
COPY tests ./tests
RUN chmod 0644 /app/main.py && \
    chmod -R a+rX /app/app /app/static /app/tests

# The public service does not require root. A fixed UID simplifies host
# permissions for the data directory.
RUN groupadd --system --gid 10001 appuser && \
    useradd --system --uid 10001 --gid appuser --home-dir /app --shell /usr/sbin/nologin appuser && \
    mkdir -p /app/data && \
    chown 10001:10001 /app/data

ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

# Document the application port.
EXPOSE 8000 443 8443

# Keep the runtime process unprivileged.
USER 10001:10001

# Start the ASGI server without trusting client-supplied proxy headers.
CMD ["python", "-m", "app.services.certmanager.server"]
