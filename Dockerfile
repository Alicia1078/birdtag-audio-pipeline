FROM public.ecr.aws/lambda/python:3.10

# Install Python packages into Lambda's task root
COPY requirements.txt .
RUN pip install -r requirements.txt --no-cache-dir --target "${LAMBDA_TASK_ROOT}"

# Copy your Lambda function code into Lambda's task root
COPY app.py ${LAMBDA_TASK_ROOT}

# Copy ffmpeg binary into /usr/local/bin inside container
COPY bin/ffmpeg /usr/local/bin/ffmpeg
RUN chmod +x /usr/local/bin/ffmpeg

# Set the Lambda handler (file.function)
CMD ["app.lambda_handler"]
