import boto3
import json
import os
import cfnresponse
from aws_lambda_powertools import Logger

logger = Logger()

config_table = os.environ.get('CONFIG_TABLE')
dynamodb = boto3.resource('dynamodb')


def write_configs(items):
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(config_table)

    with table.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)

    logger.info('write_configs completed')


@logger.inject_lambda_context
def handler(event, context=None):
    request_type = event['RequestType'] if 'RequestType' in event else None
    event_base = event['ResourceProperties'] if 'ResourceProperties' in event else event
    logger.info(f'Request type is {str(request_type)}')

    try:
        logger.debug(f'Event: {event}, Context: {context}')
        if request_type in ['Create', 'Update'] or request_type is None:
            if 'Configs' in event_base:
                items = event_base['Configs']
            elif 'DeployExampleConfigs' in event_base:
                with open('example-configs.json') as f:
                    items = json.load(f)
            else:
                raise RuntimeError("No Configs array or DeployExampleConfigs found in event")

            write_configs(items)

        else:
            logger.info('Nothing to do, request type is Delete')

        response = {
            "statusCode": "200",
            "body": json.dumps({"Status ": "SUCCESS"}),
            "headers": {
                "Content-Type": "application/json"
            }
        }

        if request_type is not None:
            data = {'Message': 'Success: {}!'.format(event['ResourceProperties']['ServiceToken'])}
            cfnresponse.send(event, context, cfnresponse.SUCCESS, data, "CustomResourcePhysicalID")

        return response

    except Exception as e:
        response = {
            "statusCode": "400",
            "body": json.dumps({"Status ": "FAILURE"}),
            "headers": {
                "Content-Type": "application/json"
            }
        }
        logger.exception('!!Failure!!')
        if request_type is not None:
            data = {'Message': 'Failure: {}!'.format(event['ResourceProperties']['ServiceToken'])}
            cfnresponse.send(event, context, cfnresponse.FAILED, data, "CustomResourcePhysicalID")
        return response
