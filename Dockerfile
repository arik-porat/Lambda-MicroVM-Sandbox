# Lambda MicroVM command runner: the managed AL2023 base image plus server.py, which runs bash
# scripts sent to the MicroVM's HTTPS endpoint (HTTP_INGRESS).
FROM public.ecr.aws/lambda/microvms:al2023-minimal

# The minimal image has no python3; server.py needs only the standard library.
RUN dnf install -y python3 && dnf clean all && python3 --version

COPY server.py /opt/command-runner/server.py

# 8900: commands (the only port callers' tokens allow). 8901: Lambda lifecycle hooks.
EXPOSE 8900 8901

# Lambda snapshots the image once server.py answers the /ready hook.
CMD ["python3", "/opt/command-runner/server.py"]
