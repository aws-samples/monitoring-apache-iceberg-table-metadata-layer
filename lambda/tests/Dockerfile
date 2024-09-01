# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /lambda

# Copy the current directory contents into the container at /app
COPY . /lambda

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r tests/requirements.txt

# Install SQLite
RUN apt-get update && \
    apt-get install -y sqlite3

ENV CW_NAMESPACE=TestNamespace \
    PYTHONPATH=/lambda

# Run the test suite
CMD ["pytest", "/lambda/tests/test_app.py"]
