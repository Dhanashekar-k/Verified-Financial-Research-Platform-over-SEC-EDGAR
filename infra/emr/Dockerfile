# EMR Serverless custom image: Spark 3.5.8 (EMR 7.14.0, arm64) + Python 3.12 + the executors'
# pinned dependencies (ADR-024). Build context is this directory:
#
#   docker build --platform linux/arm64 -t edgarqa-emr:<git-sha> infra/emr
#
# The base is pinned by DIGEST, not by the mutable `latest` tag. The application's release label
# (infra/terraform/modules/emr, `release_label`) must be emr-7.14.0 too, or EMR Serverless rejects
# the image. Pushing to ECR is done by the owner (`make emr-image-push`); nothing here pushes.
FROM public.ecr.aws/emr-serverless/spark/emr-7.14.0@sha256:ee279ec3f6618c28cdf21c1b47cced8764ec87688c356041a5e53a4629e2d7ae

USER root

# The release's default python3 is 3.9.25; the repo is Python 3.12 (ADR-024).
RUN dnf install -y python3.12 python3.12-pip \
 && dnf clean all \
 && rm -rf /var/cache/dnf

COPY requirements-emr.txt /tmp/requirements-emr.txt
RUN python3.12 -m pip install --no-cache-dir -r /tmp/requirements-emr.txt \
 && rm -f /tmp/requirements-emr.txt

# Driver and executors must run the same Python minor version (else PYTHON_VERSION_MISMATCH). The
# job run also sets these as Spark conf (pipelines/cloud/emr_run.py), which is what EMR honours.
ENV PYSPARK_PYTHON=/usr/bin/python3.12 \
    PYSPARK_DRIVER_PYTHON=/usr/bin/python3.12

# EMR Serverless runs the image as the hadoop user.
USER hadoop:hadoop
