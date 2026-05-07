FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-server \
    munge \
    slurmctld \
    slurmd \
    slurm-client \
    slurmrestd \
    slurmdbd \
    lmod \
    tini \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# curl is required by the M6 job_submit Lua plugin — slurmctld is linked
# against liblua5.1 (no socket.http / lua-cjson in jammy `main`), so the
# predictor wiring shells out to `curl --max-time` as the bound on any
# single call. See chart/templates/configmap-job-submit.yaml.

# Register /opt/modulefiles as an Lmod search path.
RUN mkdir -p /etc/lmod && echo '/opt/modulefiles' > /etc/lmod/modulespath

RUN mkdir -p /var/run/sshd /var/run/munge /var/lib/munge /var/log/munge /var/spool/slurmctld /var/spool/slurmd /etc/slurm /root/.ssh \
    && chown -R munge:munge /var/run/munge /var/lib/munge /var/log/munge \
    && chmod 700 /root/.ssh

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 22 6817
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
