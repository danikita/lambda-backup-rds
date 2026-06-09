FROM public.ecr.aws/lambda/python:3.11

# Instala dependências base
RUN yum install -y \
    unixODBC \
    unixODBC-devel \
    curl \
    openssl \
    && yum clean all

# Baixa e importa a chave GPG da Microsoft
RUN curl -L https://packages.microsoft.com/keys/microsoft.asc -o /tmp/microsoft.asc \
    && rpm --import /tmp/microsoft.asc \
    && rm /tmp/microsoft.asc

# Baixa o RPM do msodbcsql17 e instala forçando, ignorando dependências e scripts de pre/post install
RUN curl -L https://packages.microsoft.com/rhel/7/prod/msodbcsql17-17.10.6.1-1.x86_64.rpm \
        -o /tmp/msodbcsql17.rpm \
    && rpm -i --nodeps --noscripts /tmp/msodbcsql17.rpm \
    && rm /tmp/msodbcsql17.rpm

RUN pip install --no-cache-dir pyodbc boto3

COPY app.py ${LAMBDA_TASK_ROOT}
COPY config.txt ${LAMBDA_TASK_ROOT}

CMD ["app.lambda_handler"]
