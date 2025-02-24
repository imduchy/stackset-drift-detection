import json
import os

import boto3
from aws_lambda_powertools import Logger

logger = Logger()

AWS_REGION = os.environ["AWS_REGION"]
NOTIFICATION_TOPIC_ARN = os.environ["NOTIFICATION_TOPIC_ARN"]

cfn_client = boto3.client("cloudformation")
sns_client = boto3.client("sns")


def publish_to_topic(subject: str, message: dict):
    return sns_client.publish(
        TopicArn=NOTIFICATION_TOPIC_ARN,
        Subject=subject,
        Message=json.dumps({"default": message}, default=str),
    )


def lambda_handler(event, _):
    logger.info("Event received", extra={"event": event})

    detail: dict = event["detail"]
    stackset_arn: str = detail["stack-set-arn"]
    stackset_name = stackset_arn.split(":stackset/")[1].split(":")[0]
    stackset_operation_id = detail["stack-set-operation-id"]

    logger.info("Fetching the stackset operation details.")
    describe_operation_response = cfn_client.describe_stack_set_operation(
        StackSetName=stackset_name,
        OperationId=stackset_operation_id,
        CallAs="SELF",
    )
    logger.info("Details fetched.", extra={"response": describe_operation_response})

    operation_details = describe_operation_response["StackSetOperation"]
    status = operation_details["Status"]

    if status in ["FAILED", "STOPPED"]:
        status_reason = operation_details["StatusReason"]
        failed_count = operation_details["StatusDetails"]["FailedStackInstancesCount"]

        logger.error(
            "The operation didn't complete successfully!",
            extra={
                "status": status,
                "status_reason": status_reason,
                "failed_stack_instances": failed_count,
            },
        )

        publish_to_topic(
            subject=f"ERROR: StackSet {stackset_name} drift detection failed",
            message=operation_details,
        )

        return

    drift_detection_details = operation_details["StackSetDriftDetectionDetails"]
    drift_status = drift_detection_details["DriftStatus"]

    if drift_status != "IN_SYNC":
        total_count = drift_detection_details["TotalStackInstancesCount"]
        drifted_count = drift_detection_details["DriftedStackInstancesCount"]

        logger.error(
            "Stackset drift status is not in sync!",
            extra={
                "status": drift_status,
                "total_stack_instances": total_count,
                "drifted_stack_instances": drifted_count,
            },
        )

        publish_to_topic(
            subject=f"DRIFTED: StackSet {stackset_name} is in the drifted state",
            message=operation_details,
        )

        return

    logger.info(
        "Drift detection completed successfully, and all instances of %s stackset are in sync.",
        stackset_name,
    )
    return
