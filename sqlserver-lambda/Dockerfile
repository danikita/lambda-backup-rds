FROM public.ecr.aws/lambda/python:3.11

RUN yum install -y \
    unixODBC-devel \
    && curl https://packages.microsoft.com/config/rhel/7/prod.repo \
    -o /etc/yum.repos.d/mssql-release.repo \
    && ACCEPT_EULA=Y yum install -y msodbcsql18 \
    && yum clean all \
    && rm -rf /var/cache/yum

RUN pip install pyodbc --no-cache-dir

COPY app.py ${LAMBDA_TASK_ROOT}

CMD ["app.lambda_handler"]
