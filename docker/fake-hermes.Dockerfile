# A stand-in for Hermes: tools/fake_hermes.py keeps a fake, busy state.db in
# /opt/data, under a process that looks like "hermes gateway run".
# Used by docker-compose.fake.yml to try the stack without a real Hermes.
FROM python:3.12.16-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd -u 10000 -m -d /opt/data hermes
COPY tools/fake_hermes.py tools/schema.sql /fake/
USER hermes
WORKDIR /opt/data
# The trailing "gateway run" only makes the process look like a gateway.
CMD ["python3", "-c", "import runpy, sys; sys.argv = ['fake_hermes', '--db', '/opt/data/state.db', '--live']; runpy.run_path('/fake/fake_hermes.py', run_name='__main__')", "gateway", "run"]
