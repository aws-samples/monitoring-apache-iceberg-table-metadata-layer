## Monitoring Apache Iceberg Table metadata layer using AWS Lambda, AWS Glue and AWS CloudWatch

This repository provides you with sample code on how to collect metrics of an existing Apache Iceberg table managed in Amazon S3. The code consists of AWS Lambda deployment package that collects and submits metrics into AWS CloudWatch. Repository also includes helper scripts for deploying CloudWatch monitoring dashboard to visualize collected metrics.

### Technical implementation

![Architectural diagram of the solution](assets/arch.png)

* AWS Lambda triggered on every Iceberg snapshot creation to collect and send metrics to CloudWatch. This achieved by creating [S3 event notification](https://docs.aws.amazon.com/AmazonS3/latest/userguide/EventNotifications.html). See [Setting up S3 event notification](#3-setting-up-s3-event-notification) section.
* AWS Lambda code includes `pyiceberg` library and [AWS Glue interactive Sessions](https://docs.aws.amazon.com/glue/latest/dg/interactive-sessions-overview.html) with minimal compute to read `snapshots`, `partitions` and `files` Apache Iceberg metadata tables with Spark.
* AWS Lambda code aggregates information retrieved from metadata tables to create metrics and submits those to AWS CloudWatch.


### Metrics collected
*Snapshot metrics*
* snapshot.total_data_files
* snapshot.added_data_files
* snapshot.deleted_data_files
* snapshot.total_delete_files
* snapshot.added_records
* snapshot.deleted_records
* snapshot.added_files_size
* snapshot.removed_files_size
* snapshot.added_position_deletes

*Partitions aggregated metrics*
* partitions.avg_record_count
* partitions.max_record_count
* partitions.min_record_count
* partitions.deviation_record_count
* partitions.skew_record_count
* partitions.avg_file_count
* partitions.max_file_count
* partitions.min_file_count
* partitions.deviation_file_count
* partitions.skew_file_count

*Per-partition metrics*
* partitions.file_count
* partitions.record_count

*Files aggregated metrics*
* files.avg_record_count
* files.max_record_count
* files.min_record_count
* files.deviation_record_count
* files.skew_record_count
* files.avg_file_size
* files.max_file_size
* files.min_file_size

## Setup

### Prerequisites

#### Install Docker

This solution uses Docker as a dependency for AWS SAM CLI.
To install Docker follow Docker official documentation.
https://docs.docker.com/get-docker/

#### Install SAM CLI

This solution is using AWS SAM CLI to build test and deploy AWS Lambda code that collects the Iceberg table metrics and submits them into AWS CloudWatch.

To install AWS CLI follow AWS Documentation.
https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html


#### Configuring IAM permissions for AWS Glue

[Step 1: Create an IAM policy for the AWS Glue service](https://docs.aws.amazon.com/glue/latest/dg/create-service-policy.html)
[Step 2: Create an IAM role for AWS Glue](https://docs.aws.amazon.com/glue/latest/dg/create-an-iam-role.html)

### Build and Deploy

#### 1. Build AWS Lambda using AWS SAM CLI

Once you've installed [Docker](#install-docker) and [SAM CLI](#install-sam-cli) you are ready to build the AWS Lambda. Open your terminal and run command below.

```bash
sam build --use-container
```

#### 2. Deploy AWS Lambda using AWS SAM CLI

Once build is finished you can deploy your AWS Lambda. SAM will upload packaged code and deploy AWS Lambda resource using AWS CloudFormation. Run below command using your terminal.

```bash
sam deploy --guided
```

##### Parameters

- `CW_NAMESPACE` - A namespace is a container for CloudWatch metrics.
- `DBNAME` - Glue Data Catalog Database Name.
- `TABLENAME` - Apache Iceberg Table name as it appears in the Glue Data Catalog.
- `GLUE_SERVICE_ROLE` - AWS Glue Role arn you created [earlier](#configuring-iam-permissions-for-aws-glue).
- `SPARK_CATALOG_S3_WAREHOUSE` - Required catalog property to determine the root path of the data warehouse on S3. This can be any path on your S3 bucket. Not critical for the solution.


#### 3. Setting up S3 event notification

You need to setup an automatic trigger that will activate AWS Lambda metrics collection on every Apache Iceberg commit. This solution is relying on S3 event notification feature to trigger AWS Lambda every time new `metadata.json` is written to S3 `metadata` folder of the table.

You can follow AWS Documentation on how to [enable and configuring event notifications using the Amazon S3 console](https://docs.aws.amazon.com/AmazonS3/latest/userguide/enable-event-notifications.html).

or use the Python Boto3 sample code below. Replace with your bucket name and path to metadata.

```python
import boto3
s3_client = boto3.client('s3')
lambda_arn = "<REPLACE WITH YOUR ARN>"
bucket_name = "<REPLACE WITH YOUR S3 BUCKET NAME>"
path_to_metadata_folder = "<REPLACE WITH YOUR S3 PATH>"

notification_configuration = {
    'LambdaFunctionConfigurations': [
        {
            'LambdaFunctionArn': lambda_arn,
            'Events': [
                's3:ObjectCreated:Put'
            ],
            'Filter': {
                'Key': {
                    'FilterRules': [
                        {
                            'Name': 'Prefix',
                            'Value': path_to_metadata_folder
                        },
                        {
                            'Name': 'Suffix',
                            'Value': '.json'
                        }
                    ]
                }
            }
        }
    ]
}
response = s3_client.put_bucket_notification_configuration(
    Bucket=bucket_name,
    NotificationConfiguration=notification_configuration
)
if response['ResponseMetadata']['HTTPStatusCode'] == 200:
    print("Success")
else:
    print("Something went wrong")

```

The final result should look like this
![S3 to AWS Lambda trigger example](assets/trigger.png)

#### 4. (Optional) Create CloudWatch Dashboard
Once your Iceberg Table metrics are submitted to CloudWatch you can start using them to monitor and create alarms. CloudWatch also let you visualize metrics using CloudWatch Dashboards.

`assets/cloudwatch-dashboard.template.json` is a sample CloudWatch dashboard configuration that uses fraction of the submitted metrics and combines it with AWS Glue native metrics for Apache Iceberg. 
We use Jinja2 so you could generate your own dashboard by providing your parameters.

TODO: include dashboard screenshot

Run the script below to generate your own CloudWatch dashboard configuration.
Replace input values with the relevant [parameters](#parameters) from previous sections.

```python
import json
from jinja2 import Template

def render_json_template(template_path, data):
    with open(template_path, 'r') as file:
        template_text = file.read()

    template = Template(template_text)
    rendered_json = template.render(data)
    json_data = json.loads(rendered_json)
    return json_data

# Data to fill in the template
data = {
    "CW_NAMESPACE": "<<REPLACE>>",
    "REGION": "<<REPLACE>>",
    "DBNAME": "<<REPLACE>>",
    "TABLENAME": "<<REPLACE>>"
}

# Path to cloudwatch template file
template_path = 'assets/cloudwatch-dashboard.template.json'
rendered_data = render_json_template(template_path, data)
output_path = 'assets/cloudwatch-dashboard.rendered.json'

with open(output_path, 'w') as file:
        json.dump(rendered_data, file, indent=4)

print(f"Your dashboard configuration successfully generated at {output_path}")
```

Now follow steps to create CloudWatch dashboard from rendered json.

1. Sign in to the AWS Management Console and navigate to the CloudWatch service.
2. In the navigation pane, click on "Dashboards" on the left pane.
3. Click on "Create Dashboard" and give it a name. 
4. If widget configuration popup appears click "Cancel".
5. Click the "Actions" dropdown menu in the top right corner of the dashboard and select "View/edit source".
This will open a new tab with the source JSON for the dashboard. You can then paste rendered JSON into a Dashboard source to create a custom dashboard resource.
6. Click "Update"
7. The new dashboard supposedly empty. Once your AWS Lambda will generate metrics they will appear here.

### Test Locally

You can test the code locally on using SAM CLI.
Ensure you have configured the [right AWS permissions](https://docs.aws.amazon.com/cli/latest/userguide/cli-chap-configure.html) to call CloudWatch and AWS Glue.

```bash
sam local invoke IcebergMetricsLambda --env-vars .env.local.json
```

`.env.local.json` - The JSON file that contains values for the Lambda function's environment variables. Lambda code is dependent on env vars that you are passing in the deploy section. You need to create the file it and include relevant [parameters](#parameters) before you calling `sam local invoke`.


## Dependencies

PyIceberg is a Python implementation for accessing Iceberg tables, without the need of a JVM. \
https://py.iceberg.apache.org

AWS Serverless Application Model (AWS SAM) \
https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/what-is-sam.html

Docker \
https://docs.docker.com/get-docker/

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.

