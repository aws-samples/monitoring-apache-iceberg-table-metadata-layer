FROM public.ecr.aws/lambda/python:3.10

COPY . ${LAMBDA_TASK_ROOT}

# Install the function's dependencies
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

ARG CLOUDWATCH_NAMESPACE
ENV CW_NAMESPACE=$CLOUDWATCH_NAMESPACE

CMD [ "app.lambda_handler" ]