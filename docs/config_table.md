## Pipeline Config table

The pipeline config table contains items with configuration data related to:   
- Hudi pipelines and related configs
- Tables and related configs

### Partition Key/Sort Key

The PK/SK are:

```
identifier ( STRING, Partition key )
config  (STRING, Sort key)
```

- *identifier*: This attribute is meant to uniquely identify a group of config records related to a set of pipelines.   
  - ***This must match `{{resolve:secretsmanager:${DatabaseSecret}:SecretString:dbname}}` from the Secret 
- *config*: This attribute must be one of `[pipeline::hudi_bulk_insert, pipeline::hudi_upsert, pipeline::hudi_continuous, table::<schema.table name>]`.

#### Item specifics

In order for the system to function, each `identifier` group of items must have at least 2 `pipeline` items.
Using `hammerdb` as our identifier(source database name), here is an example of the minimum number of items in DynamoDB if you just wanted to load the customer table:   

|identifier|config|
|----------|------|
|hammerdb|pipeline::hudi_bulk_insert|
|hammerdb|pipeline::hudi_upsert|
|hammerdb|table::public.customer|


##### Item schema

Each config entry type has its own expected schema

#### Pipelines 

Pipeline items format is `pipeline::<pipeline type>`.

##### Item format

`pipeline::` items are meant to tell the system how to launch the EMR cluster. (EG: Number of nodes, node type, parallelism, etc)

```
{
    "config": "pipeline::<pipeline type>", // Type and name of the pipeline
    "identifier": "<DatabaseName>",
    "emr_config": {                             // These configs get passed into the StepFunction and are used when creating the EMR cluster
                                                // At present, cluster instance options are limited, need to add Spot and Autoscaling support
        "release_label": "emr-6.7.0",
        "master": {
            "instance_type": "m5.xlarge"
        },
        "worker": {
            "count": "6",
            "instance_type": "r5.2xlarge"
        },
        "maximize_resource_allocation": "[true|false]", // Whether or not to set the EMR-specific "maximizeResourceAllocation" 

        "step_parallelism": 4                   // NOTE: Make sure your worker settings can handle the parallelism you choose
                                                //       This is especially important for continuous_load
    }
}
```

#### table::<table.name>

`table::` items instruct the system about how to process each table. This translates into one EMR job step per table. 

#####  table:: item schema

The attributes are as follows. Some attributes can be omitted, su

```
{
    "config": "table::<schema.table_name>",       // One item per table
    "identifier": "<DatabaseName>",
    "enabled": [true|false],                      // Enable or Disable the table 
    "hudi_config": {
        "primary_key": "<c,s,v>",               // Comma-separated list of primary key columns on the table       
        "watermark": "trx_seq",                 // The "tie-breaker" column, used as the precombine field for merging rows in Hudi
                                                // Currently only trx_seq (AR_H_CHANGE_SEQ from AWS DMS) is supported
                                                // https://aws.amazon.com/blogs/database/capture-key-source-table-headers-data-using-aws-dms-and-use-it-for-amazon-s3-data-lake-operations/
        "is_partitioned": [true|false],         // Whether or not to partition the Hudi table
        "partition_path": "<column>",           // Required only if is_partitioned is true, the column to partition on
        "partition_extractor_class": "<cls>"    // The Hive partition extractor class EG: org.apache.hudi.hive.MultiPartKeysValueExtractor
        "transformer_class": "<cls"             // [OPTIONAL] The DeltaStreamer partition class EG: org.apache.hudi.utilities.transform.SqlQueryBasedTransformer
        "transformer_sql": "<SQL STATEMENT AS s>// [OPTIONAL] The sql statement (must be set if transformer_class is SqlQueryBasedTransformer
    },
    "spark_conf": {                             // [OPTIONAL] this stanza is used to pass spark configurations to the emr job step
                                                // Any option found here can be set:  https://spark.apache.org/docs/latest/configuration.html#available-properties
        "<pipeline_type>": {                    // <pipeline type> must be one of: [ hudi_bulk_insert|hudi_upsert|hudi_upsert_continuous ]
            "<option>": "<value"
        }
    }
}
```

Example record for public.order_line in rdbms_analytics grouping:    

```
{
    "config": "table::public.order_line",
    "identifier": "hammerdb",
    "enabled": true,
    "hudi_config": {
        "primary_key": "ol_w_id,ol_d_id,ol_o_id,ol_number",
        "watermark": "trx_seq",
        "is_partitioned": true,
        "partition_path": "ol_w_id",
        "partition_extractor_class": "org.apache.hudi.hive.MultiPartKeysValueExtractor"
    },
    "spark_conf": {
        "hudi_bulk_insert": {
            "spark.executor.cores": "2",
            "spark.executor.memory": "8g",
            "spark.executor.heartbeatInterval": "90s",
            "spark.network.timeout": "900s",
            "spark.dynamicAllocation.enabled": "false",
            "spark.executor.instances": "12"
        },
        "hudi_upsert": {
            "spark.executor.cores": "2",
            "spark.executor.memory": "6g",
            "spark.executor.heartbeatInterval": "30s",
            "spark.network.timeout": "600s",
            "spark.dynamicAllocation.minExecutors": "2",
            "spark.dynamicAllocation.maxExecutors": "12"
        },
        "hudi_upsert_continuous": {
            "spark.executor.cores": "1",
            "spark.executor.memory": "4g",
            "spark.executor.heartbeatInterval": "30s",
            "spark.network.timeout": "300s",
            "spark.dynamicAllocation.minExecutors": "2",
            "spark.dynamicAllocation.maxExecutors": "6"
        }
    }
}
```
