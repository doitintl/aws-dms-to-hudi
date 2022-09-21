import boto3
import json
import os
import backoff
from datetime import datetime
from boto3.dynamodb.conditions import Key
from aws_lambda_powertools import Logger

logger = Logger()

sfn_arn = os.environ.get('STEPFUNCTION_ARN')
config_table = os.environ.get('CONFIG_TABLE')
raw_lake_uri = os.environ['RAW_LAKE_S3URI']
curated_lake_uri = os.environ['CURATED_LAKE_S3URI']
glue_database = os.environ.get('GLUE_DATABASE', 'default')

sfn_client = boto3.client('stepfunctions')
dynamodb = boto3.resource('dynamodb')


def munge_configs(items, pipeline_type):
    configs = {
        'StepConfigs': {},
        'PipelineConfig': {}
    }

    # Check if pipeline type is supported, otherwise raise
    if pipeline_type not in ['hudi_bulk_insert', 'incremental_hudi', 'continuous_hudi']:
        raise ValueError(f'Operation {pipeline_type} not yet supported.')

    for i in items:
        if i['config'] == f'pipeline::{pipeline_type}':
            configs['PipelineConfig'] = i
            configs['PipelineConfig']['emr_config']['step_parallelism'] = int(i['emr_config']['step_parallelism'])
            configs['PipelineConfig']['emr_config']['worker']['count'] = int(i['emr_config']['worker']['count'])
            if 'maximize_resource_allocation' not in i['emr_config']:
                configs['PipelineConfig']['emr_config']['maximize_resource_allocation'] = 'false'
        elif i['config'].startswith('table::'):
            step_name = i['config'].split('::')[-1]
            configs['StepConfigs'][step_name] = i
            logger.info(f'Step {step_name} detected pipeline {pipeline_type}')
    return configs


def get_configs(identifier, pipeline_type):
    table = dynamodb.Table(config_table)
    items = []
    response = table.query(
        KeyConditionExpression=Key('identifier').eq(identifier)
    )
    for item in response['Items']:
        items.append(item)

    while 'LastEvaluatedKey' in response:
        response = table.query(
            KeyConditionExpression=Key('identifier').eq(identifier),
            ExclusiveStartKey=response['LastEvaluatedKey']
        )
        for item in response['Items']:
            items.append(item)

    configs = munge_configs(items, pipeline_type)
    return configs


def get_hudi_configs(identifier, table_name, table_config, pipeline_type):
    record_key = table_config['record_key']
    precombine_field = table_config['source_ordering_field']

    source_s3uri = os.path.join(raw_lake_uri, identifier, table_name.replace('_', '/', 2), '')

    hudi_conf = {
        'hoodie.clustering.inline': 'true',
        'hoodie.archive.merge.enable': 'true',
        'hoodie.table.name': table_name,
        'hoodie.datasource.write.recordkey.field': record_key,
        'hoodie.datasource.write.precombine.field': precombine_field,
        'hoodie.datasource.hive_sync.database': glue_database,
        'hoodie.datasource.hive_sync.enable': 'true',
        'hoodie.datasource.hive_sync.table': table_name,
        'hoodie.datasource.clustering.inline.enable': 'true',
        'hoodie.deltastreamer.source.dfs.root': source_s3uri
    }

    if pipeline_type == 'hudi_bulk_insert':
        hudi_conf['hoodie.datasource.write.operation'] = 'bulk_insert'
        hudi_conf['hoodie.bulkinsert.sort.mode'] = 'PARTITION_SORT'
    elif pipeline_type.startswith('hudi_upsert'):
        hudi_conf['hoodie.datasource.write.operation'] = 'upsert'
        hudi_conf['hoodie.cleaner.commits.retained'] = '5'
        hudi_conf['hoodie.clean.automatic'] = 'true'
        hudi_conf['hoodie.keep.min.commits'] = '10'
        hudi_conf['hoodie.keep.max.commits'] = '15'
    else:
        raise ValueError(f'Operation {pipeline_type} not yet supported.')

    if table_config['is_partitioned'] is False:
        partition_extractor = 'org.apache.hudi.hive.NonPartitionedExtractor'
        key_generator = 'org.apache.hudi.keygen.NonpartitionedKeyGenerator'
    else:
        partition_extractor = table_config['partition_extractor_class']
        hudi_conf['hoodie.datasource.write.hive_style_partitioning'] = 'true'
        hudi_conf['hoodie.datasource.write.partitionpath.field'] = table_config['partition_path']
        hudi_conf['hoodie.datasource.hive_sync.partition_fields'] = table_config['partition_path']
        if len(record_key.split(',')) > 1:
            key_generator = 'org.apache.hudi.keygen.ComplexKeyGenerator'
        else:
            key_generator = 'org.apache.hudi.keygen.SimpleKeyGenerator'

    hudi_conf['hoodie.datasource.write.keygenerator.class'] = key_generator

    if 'table_type' in table_config and table_config['table_type'] == 'MERGE_ON_READ':
        hudi_conf['hoodie.compact.inline'] = 'true'

    if 'transformer_sql' in table_config:
        hudi_conf['hoodie.deltastreamer.transformer.sql'] = table_config['transformer_sql']

    hudi_conf['hoodie.datasource.hive_sync.partition_extractor_class'] = partition_extractor

    logger.debug(json.dumps(hudi_conf, indent=4))

    return hudi_conf


def generate_steps(identifier, configs, pipeline_type):
    steps = []
    for table in configs['StepConfigs'].keys():
        spark_submit_args = ['spark-submit']
        config = configs['StepConfigs'][table]
        logger.debug(json.dumps(config, indent=4))
        if 'enabled' in config and config['enabled'] is True:
            table_name = table.replace('.', '_')

            if 'spark_conf' in config and pipeline_type in config['spark_conf']:
                for k, v in config['spark_conf'][pipeline_type].items():
                    spark_submit_args.extend(['--conf', f'{k}={v}'])

            spark_submit_args.extend([
                '--class', 'org.apache.hudi.utilities.deltastreamer.HoodieDeltaStreamer',
                '/usr/lib/hudi/hudi-utilities-bundle.jar',
                '--source-class', 'org.apache.hudi.utilities.sources.ParquetDFSSource',
                '--enable-sync',
                '--target-table', table_name,
                '--target-base-path', os.path.join(curated_lake_uri, identifier, table_name, ''),
                '--source-ordering-field', config['hudi_config']['source_ordering_field']
            ])

            if 'table_type' in config['hudi_config']:
                table_type = config['hudi_config']['table_type']
            else:
                table_type = 'COPY_ON_WRITE'
            spark_submit_args.extend(['--table-type', table_type])

            if 'transformer_class' in config['hudi_config']:
                spark_submit_args.extend(['--transformer-class', config['hudi_config']['transformer_class']])

            if pipeline_type == 'hudi_bulk_insert':
                spark_submit_args.extend(['--op', 'BULK_INSERT'])
            elif 'op' in config['hudi_config']:
                spark_submit_args.extend(['--op', config['hudi_config']['op']])

            hudi_configs = get_hudi_configs(identifier, table_name, config['hudi_config'], pipeline_type)
            for k, v in hudi_configs.items():
                spark_submit_args.extend(['--hoodie-conf', f'{k}={v}'])

            if pipeline_type == 'hudi_upsert_continuous':
                spark_submit_args.extend(['--continuous'])

            entry = {
                'step_name': table,
                'jar_step_args': spark_submit_args
            }
            steps.append(entry)
            logger.info(f'Table added to stepfunction input: {json.dumps(entry, indent=4)}')
        else:
            logger.info(f'Table {table} is disabled, skipping. To enable, set attribute "enabled": true')
            continue

    return steps


def generate_sfn_input(identifier, configs, pipeline_type, lambda_context):
    steps = generate_steps(identifier, configs, pipeline_type)

    if len(steps) == 0:
        raise RuntimeError(f'No steps have been generated based on {pipeline_type}. Ensure they are configured and enabled.')

    sfn_input = {
        'lambda': {
            'function_name': lambda_context.function_name,
            'identifier': identifier,
            'pipeline_type': pipeline_type,
            'steps': steps,
            'pipeline': configs['PipelineConfig'],
            'log_level': os.environ.get('LOG_LEVEL', 'INFO')
        }
    }
    return sfn_input


@backoff.on_exception(backoff.expo, exception=RuntimeError, max_time=60)
def check_concurrent():
    paginator = sfn_client.get_paginator('list_executions')
    pages = paginator.paginate(
        stateMachineArn=sfn_arn,
        statusFilter='RUNNING'
    )
    for page in pages:
        for execution in page['executions']:
            exec_arn = execution['executionArn']
            raise RuntimeError(
                f"Pipeline cannot run due to in-progress pipeline {exec_arn}"
            )


def launch_sfn(execution_id, sfn_input):
    response = sfn_client.start_execution(
        stateMachineArn=sfn_arn,
        name=execution_id,
        input=json.dumps(sfn_input)
    )
    return response


@logger.inject_lambda_context
def handler(event, context=None):
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    identifier = event['Identifier']
    pipeline_type = event['PipelineType']

    config_dict = get_configs(identifier, pipeline_type)
    check_concurrent()

    execution_id = f'{identifier}-{pipeline_type}-{timestamp}'
    sfn_input = generate_sfn_input(identifier, config_dict, pipeline_type, context)
    response = launch_sfn(execution_id, sfn_input)

    return {
        "statusCode": response['ResponseMetadata']['HTTPStatusCode'],
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps({
            "executionArn ": response['executionArn']
        })
    }
