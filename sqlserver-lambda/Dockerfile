FROM public.ecr.aws/lambda/python:3.11

# Instala dependências base
RUN yum install -y \
    unixODBC \
    unixODBC-devel \
    curl \
    && yum clean all

# Baixa e importa a chave GPG da Microsoft
RUN curl -L https://packages.microsoft.com/keys/microsoft.asc -o /tmp/microsoft.asc \
    && rpm --import /tmp/microsoft.asc \
    && rm /tmp/microsoft.asc

# Adiciona o repositório da Microsoft
RUN curl -L https://packages.microsoft.com/config/rhel/7/prod.repo \
        -o /etc/yum.repos.d/mssql-release.repo

# Remove pacote conflitante e instala o driver ODBC 17
RUN yum remove -y openssl-snapsafe-libs \
    && ACCEPT_EULA=Y yum install -y --nogpgcheck msodbcsql17 \
    && yum clean all

RUN pip install --no-cache-dir pyodbc boto3

COPY app.py ${LAMBDA_TASK_ROOT}
COPY config.txt ${LAMBDA_TASK_ROOT}

CMD ["app.lambda_handler"]
