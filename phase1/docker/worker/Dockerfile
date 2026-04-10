FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-server \
    munge \
    slurmd \
    slurm-client \
    openmpi-bin \
    libopenmpi-dev \
    lmod \
    tini \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Register /opt/modulefiles as an Lmod search path.
# /etc/lmod/modulespath is read by lmod.sh on EVERY shell invocation (login and
# non-login), so batch jobs that do "source /etc/profile.d/lmod.sh" will find
# the ConfigMap-mounted modulefiles without needing an explicit MODULEPATH export.
RUN mkdir -p /etc/lmod && echo '/opt/modulefiles' > /etc/lmod/modulespath

RUN mkdir -p /var/run/sshd /var/run/munge /var/lib/munge /var/log/munge /var/spool/slurmd /etc/slurm /root/.ssh \
    && chown -R munge:munge /var/run/munge /var/lib/munge /var/log/munge \
    && chmod 700 /root/.ssh

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 22 6818
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
