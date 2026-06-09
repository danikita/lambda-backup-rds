FROM public.ecr.aws/lambda/python:3.11

# Desabilita GPG check (acelera MUITO)
RUN yum install -y yum-utils \
    && yum-config-manager --add-repo https://packages.microsoft.com/config/rhel/7/prod.repo \
    && sed -i 's/gpgcheck=1/gpgcheck=0/g' /etc/yum.repos.d/microsoft-prod.repo \
    && yum install -y unixODBC-devel msodbcsql18 \
    && yum clean all \
    && rm -rf /var/cache/yum

# Python deps
RUN pip install --no-cache-dir pyodbc boto3

COPY app.py ${LAMBDA_TASK_ROOT}

CMD ["app.lambda_handler"]
