FROM public.ecr.aws/lambda/python:3.11

# Instala dependências do sistema (cliente MySQL + mysqldump)
RUN yum install -y mysql

# Instala dependências Python
RUN pip install boto3

# Copia o script para o container
COPY app.py ./

# Define o ponto de entrada
CMD ["app.lambda_handler"]
