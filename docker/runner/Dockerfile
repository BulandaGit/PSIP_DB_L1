FROM docker:cli AS docker_cli
FROM ghcr.io/actions/actions-runner:latest

USER root
COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker_cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose
COPY docker/runner/entrypoint.sh /usr/local/bin/psip-runner
RUN chmod +x /usr/local/bin/psip-runner

ENV RUNNER_ALLOW_RUNASROOT=1
WORKDIR /runner
ENTRYPOINT ["/usr/local/bin/psip-runner"]
CMD ["run"]
