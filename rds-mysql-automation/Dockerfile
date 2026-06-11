FROM public.ecr.aws/lambda/python:3.11

RUN yum install -y mysql

RUN pip install boto3

COPY app.py ./

CMD ["app.lambda_handler"]
