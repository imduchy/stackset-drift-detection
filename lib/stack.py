import json
from typing import Optional

import aws_cdk.aws_events as events
import aws_cdk.aws_events_targets as targets
import aws_cdk.aws_iam as iam
import aws_cdk.aws_lambda as _lambda
import aws_cdk.aws_lambda_destinations as destinations
import aws_cdk.aws_logs as logs
import aws_cdk.aws_scheduler as scheduler
import aws_cdk.aws_sns as sns
import aws_cdk.aws_sns_subscriptions as subscriptions
import aws_cdk.aws_sqs as sqs
from aws_cdk import Duration, RemovalPolicy, Stack
from constructs import Construct


class StacksetDriftDetectionStackProps:
    def __init__(
        self,
        stackset_names: list[str],
        schedule_expression: str,
        notification_email_endpoints: Optional[list[str]] = None,
        notification_https_endpoints: Optional[list[str]] = None,
    ):
        """
        :param stackset_names: A list of StackSet names to monitor for drift detection.
        :param schedule_expression: A `cron` or `rate` schedule expression passed directly to the
          scheduler resource. See the official CDK documentation for more information
          https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_scheduler/CfnSchedule.html#cfnschedule
        :param notification_email_endpoints: A list of email addresses to subscribe to the SNS topic
        :param notification_https_endpoints: A list of HTTPS endpoints to subscribe to the SNS topic
        """
        self.stackset_names = stackset_names
        self.schedule_expression = schedule_expression
        self.notification_email_endpoints = notification_email_endpoints or []
        self.notification_https_endpoints = notification_https_endpoints or []


class StacksetDriftDetectionStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        props: StacksetDriftDetectionStackProps,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self._props = props

        self._notification_topic = self._create_notification_topic()
        self._schedulers = self._create_schedulers()
        self._evaluation_function = self._create_evaluation_function(self._notification_topic)
        self._drift_status_rule = self._create_drift_status_eb_rule()
        self._drift_status_rule.add_target(targets.LambdaFunction(self._evaluation_function))

    def _create_notification_topic(self):
        topic = sns.Topic(
            self,
            "DeliveryTopic",
            enforce_ssl=True,
        )

        for email in self._props.notification_email_endpoints:
            topic.add_subscription(
                subscriptions.EmailSubscription(
                    email_address=email,
                    json=True,
                )
            )

        for endpoint in self._props.notification_https_endpoints:
            topic.add_subscription(
                subscriptions.UrlSubscription(
                    url=endpoint,
                    protocol=sns.SubscriptionProtocol.HTTPS,
                )
            )

        return topic

    def _create_schedulers(self):
        dead_letter_queue = sqs.Queue(
            self,
            "SchedulerCommonDLQ",
            enforce_ssl=True,
        )

        role = iam.Role(
            self,
            "SchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
            inline_policies={
                "Default": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            effect=iam.Effect.ALLOW,
                            actions=["cloudformation:DetectStackSetDrift"],
                            resources=["*"],
                        ),
                        iam.PolicyStatement(
                            effect=iam.Effect.ALLOW,
                            actions=["sqs:SendMessage"],
                            resources=[dead_letter_queue.queue_arn],
                        ),
                    ]
                )
            },
        )

        group = scheduler.CfnScheduleGroup(
            self,
            "SchedulerGroup",
        )

        schedules = []

        for stackset_name in self._props.stackset_names:
            schedule = scheduler.CfnSchedule(
                self,
                f"Scheduler{stackset_name}",
                group_name=group.name,
                target=scheduler.CfnSchedule.TargetProperty(
                    arn="arn:aws:scheduler:::aws-sdk:cloudformation:detectStackSetDrift",
                    input=json.dumps(
                        {
                            "StackSetName": stackset_name,
                            "OperationPreferences": {
                                "RegionConcurrencyType": "PARALLEL",
                                "MaxConcurrentCount": 10,
                                "ConcurrencyMode": "SOFT_FAILURE_TOLERANCE",
                            },
                        }
                    ),
                    role_arn=role.role_arn,
                    dead_letter_config=scheduler.CfnSchedule.DeadLetterConfigProperty(
                        arn=dead_letter_queue.queue_arn
                    ),
                ),
                schedule_expression=self._props.schedule_expression,
                flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            )

            schedules.append(schedule)

        return schedules

    def _create_evaluation_function(self, notifications_topic: sns.Topic):
        log_group = logs.LogGroup(
            self,
            "EvaluationFunctionLogGroup",
            removal_policy=RemovalPolicy.DESTROY,
            retention=logs.RetentionDays.ONE_MONTH,
        )

        function = _lambda.Function(
            self,
            "EvaluationFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="evaluation.lambda_handler",
            code=_lambda.Code.from_asset("lib/lambda"),
            environment={
                "NOTIFICATION_TOPIC_ARN": notifications_topic.topic_arn,
                "POWERTOOLS_SERVICE_NAME": "evaluation",
                "POWERTOOLS_LOG_LEVEL": "INFO",
            },
            log_group=log_group,
            logging_format=_lambda.LoggingFormat.JSON,
            timeout=Duration.seconds(5),
            # If the function fails, send the message to the same (notifications) SNS topic
            on_failure=destinations.SnsDestination(notifications_topic),
            layers=[
                _lambda.LayerVersion.from_layer_version_arn(
                    self,
                    "PowertoolsLambdaLayer",
                    f"arn:aws:lambda:{self.region}:017000801446:layer:AWSLambdaPowertoolsPythonV3-python312-x86_64:8",
                )
            ],
        )

        function.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["cloudformation:DescribeStackSetOperation"],
                resources=["*"],
            )
        )

        function.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["sns:Publish"],
                resources=[notifications_topic.topic_arn],
            )
        )

        return function

    def _create_drift_status_eb_rule(self):
        stackset_arns = [
            f"arn:aws:cloudformation:{self.region}:{self.account}:stackset/{stackset_name}:*"
            for stackset_name in self._props.stackset_names
        ]

        rule = events.Rule(
            self,
            "DriftDetectionEventRule",
            description="An EventBridge rule matching failed CloudFormation StackSet drift events",
            enabled=True,
            event_pattern=events.EventPattern(
                detail_type=["CloudFormation StackSet Operation Status Change"],
                source=["aws.cloudformation"],
                detail={
                    "action": ["DETECT_DRIFT"],
                    "stack-set-arn": [{"wildcard": arn} for arn in stackset_arns],
                    "status-details": {"status": ["SUCCEEDED", "FAILED", "STOPPED"]},
                },
            ),
        )

        return rule

    @property
    def notification_topic(self):
        return self._notification_topic

    @property
    def schedulers(self):
        return self._schedulers

    @property
    def drift_status_rule(self):
        return self._drift_status_rule

    @property
    def evaluation_function(self):
        return self._evaluation_function
