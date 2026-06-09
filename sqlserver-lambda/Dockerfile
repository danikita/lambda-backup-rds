FROM public.ecr.aws/lambda/python:3.11

# Instala dependências base
RUN yum install -y \
    unixODBC \
    unixODBC-devel \
    curl \
    && yum clean all

# Adiciona o repositório oficial da Microsoft e instala o driver ODBC 17
RUN curl -L https://packages.microsoft.com/config/rhel/7/prod.repo \
        -o /etc/yum.repos.d/mssql-release.repo \
    && ACCEPT_EULA=Y yum install -y msodbcsql17 \
    && yum clean all

RUN pip install --no-cache-dir pyodbc boto3

COPY app.py ${LAMBDA_TASK_ROOT}
COPY config.txt ${LAMBDA_TASK_ROOT}

CMD ["app.lambda_handler"]
