FROM public.ecr.aws/lambda/python:3.11

RUN yum install -y yum-utils unixODBC unixODBC-devel \
    && yum clean all

# Instala o driver 17 forçando, sem dependências e sem scripts
RUN curl -L https://packages.microsoft.com/rhel/7/prod/msodbcsql17-17.10.6.1-1.x86_64.rpm \
        -o /tmp/msodbcsql17.rpm \
    && rpm -i --nodeps --noscripts --nosignature /tmp/msodbcsql17.rpm \
    && rm /tmp/msodbcsql17.rpm

# Registra o driver manualmente no odbcinst.ini
RUN echo "[ODBC Driver 17 for SQL Server]" > /etc/odbcinst.ini \
    && echo "Description=Microsoft ODBC Driver 17 for SQL Server" >> /etc/odbcinst.ini \
    && echo "Driver=/opt/microsoft/msodbcsql17/lib64/libmsodbcsql-17.10.so.6.1" >> /etc/odbcinst.ini \
    && echo "UsageCount=1" >> /etc/odbcinst.ini

# Confirma instalação
RUN odbcinst -q -d | grep -i "ODBC Driver 17" || (echo "DRIVER 17 NAO ENCONTRADO!" && exit 1)

RUN pip install --no-cache-dir pyodbc boto3

COPY app.py ${LAMBDA_TASK_ROOT}

CMD ["app.lambda_handler"]
