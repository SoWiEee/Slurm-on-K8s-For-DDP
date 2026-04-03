FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-server \
    munge \
    slurmctld \
    slurmd \
    slurm-client \
    slurmrestd \
    tini \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /var/run/sshd /var/run/munge /var/lib/munge /var/log/munge /var/spool/slurmctld /var/spool/slurmd /etc/slurm /root/.ssh \
    && chown -R munge:munge /var/run/munge /var/lib/munge /var/log/munge \
    && chmod 700 /root/.ssh

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 22 6817
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
