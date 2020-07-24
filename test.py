import io
import json
import os
from datetime import datetime

import requests
from airflow import DAG
from airflow.operators.python_operator import PythonOperator
from strela import cdc_to_s3, default_dag_args
from strela.hooks.snowflake_hook import SnowflakeHook


def all_users_download(buf):
    url = "http://solr1-b:49332/exchange.json"

    with requests.get(url) as r:
        r.raise_for_status()
        json_result = r.json()
        json.dump(json_result, buf)
        return json_result["timestamp"]


def exchange_rates_to_s3():
    with io.StringIO() as buf:
        timestamp = all_users_download(buf)
        key = "open_exchange_rates"
        s3_key = "{}/{}".format(key, timestamp)
        path = cdc_to_s3(s3_key, buf)
        return path.split("/", 1)[1]


def merge_into_table(**context):
    prefix = context.get("task_instance").xcom_pull(context["previous_task_id"])
    assert prefix

    stage_bucket = os.environ["STRELA__SNOWFLAKE_CDC_STAGE"] + "/vendors"

    query = """
        MERGE INTO WEEBLY_DATA.PUBLIC.OPEN_EXCHANGE_RATES T1 USING (

        WITH STAGE_CTE AS (               
                SELECT  src.$1['timestamp']::NUMBER AS EPOCH,
                        lf.key AS CURRENCY,
                         lf.value::FLOAT as RATE 
                FROM
                @{stage_bucket}/{prefix}
                (FILE_FORMAT=> 'WEEBLY_DATA.APP.VENDORS_JSON') src
                , lateral flatten (input => $1['rates']) lf
        ) SELECT TO_TIMESTAMP(EPOCH)::DATE AS DATE, CURRENCY, RATE  FROM STAGE_CTE

      ) T2 ON T1.DATE = T2.DATE AND T1.CURRENCY = T2.CURRENCY 
      WHEN NOT MATCHED THEN INSERT (DATE, CURRENCY, RATE, ETL_LOAD_TIMESTAMP )
       VALUES (DATE, CURRENCY, RATE, CURRENT_TIMESTAMP::TIMESTAMP)
    """.format(
        stage_bucket=stage_bucket, prefix=prefix
    )

    hook = SnowflakeHook(
        snowflake_conn_id="weebly_snowflake",
        account=os.environ["STRELA__SNOWFLAKE_ACCOUNT"],
        database=os.environ["STRELA__SNOWFLAKE_DATABASE"],
    )

    with hook.get_conn().cursor() as cursor:
        print(query.strip())
        cursor.execute(query)
        print("Query ran, inserted {} rows".format(cursor.rowcount))


def open_exchange_rates_dag():
    dag = DAG(
        "open_exchange_rates",
        catchup=False,
        default_args=default_dag_args(start_date=datetime(2019, 4, 16, 1)),
        schedule_interval="30 1 * * *",
    )

    t1 = PythonOperator(
        task_id="open_exchange_rates_to_s3",
        python_callable=exchange_rates_to_s3,
        dag=dag,
    )

    t2 = PythonOperator(
        task_id="merge_into_table",
        python_callable=merge_into_table,
        op_kwargs={"previous_task_id": t1.task_id},
        provide_context=True,
        dag=dag,
    )

    t1 >> t2

    return dag


dag = open_exchange_rates_dag()