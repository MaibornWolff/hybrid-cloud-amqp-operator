FROM alpine:3.15 as helm
# Download helm binary and charts, use extra stage to keep final image small
RUN wget -O - https://get.helm.sh/helm-v3.8.1-linux-amd64.tar.gz | tar -xzO linux-amd64/helm > /helm
RUN chmod +x /helm \
    && /helm repo add bitnami https://charts.bitnami.com/bitnami \
    && /helm pull bitnami/rabbitmq --untar --version 10.1.9 --destination /charts

FROM python:3.10 as base
# Download requirements from the full image as git is required but not contained in the slim image
COPY requirements.txt /operator/requirements.txt
RUN pip install -r /operator/requirements.txt && rm -rf /root/.cache/pip

FROM python:3.10-slim
RUN mkdir /operator
WORKDIR /operator
# Install python dependencies
COPY --from=base /usr/local/lib/python3.10/site-packages/ /usr/local/lib/python3.10/site-packages/
COPY --from=base /usr/local/bin/kopf /usr/local/bin/kopf
# Copy downloaded helm charts
COPY --from=helm /charts /operator/charts
COPY --from=helm /helm /usr/local/bin/helm
# Copy operator code
COPY main.py /operator/
COPY hybridcloud /operator/hybridcloud
# Switch to extra user
RUN useradd -M -U -u 1000 hybridcloud && chown -R hybridcloud:hybridcloud /operator
USER 1000:1000
CMD ["kopf", "run", "--liveness=http://0.0.0.0:8080/healthz", "main.py", "-A"]
